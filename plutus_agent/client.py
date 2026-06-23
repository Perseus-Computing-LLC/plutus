"""Embeddable client — the one-import way to meter agent spend from your code.

**Local mode** (default) holds its own SQLite connection — pure offline, no
network, safe to drop into any agent hot path:

    from plutus_agent import Meter
    plutus = Meter(org="Acme Agents")          # created if it doesn't exist
    plutus.track(provider="anthropic", model="claude-opus-4-8",
                 input_tokens=1200, output_tokens=800, task_type="code_review")
    print(plutus.balance())                     # remaining prepaid credit

**Remote mode** sends each event to a hosted Plutus over ``POST /v1/usage`` with
an API key — no local database, so many machines report into one dashboard:

    plutus = Meter(remote="https://plutus.perseus.observer",
                   api_key="plutus_sk_…")       # or env PLUTUS_REMOTE_URL / PLUTUS_API_KEY
    plutus.track(provider="anthropic", model="claude-opus-4-8",
                 input_tokens=1200, output_tokens=800)

Remote mode is auto-detected from ``PLUTUS_REMOTE_URL`` + ``PLUTUS_API_KEY`` when
no ``remote=`` is passed, so the same code (and the bundled adapters / Claude
Code hook) reports locally or to a hosted instance just by setting two env vars.
``track()`` returns the same :class:`~plutus_agent.metering.MeterResult` either
way; ``balance()`` / ``summary()`` / ``topup()`` are local-only.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from . import config as cfgmod, db, metering


class PlutusError(RuntimeError):
    """A remote ingest call failed (network or non-auth HTTP error)."""


class PlutusAuthError(PlutusError):
    """The remote instance rejected the API key (401)."""


class Meter:
    def __init__(self, org: Optional[str] = None, *, tier: str = "free",
                 db_path: Optional[str] = None, config: Optional[dict] = None,
                 create: bool = True, remote: Optional[str] = None,
                 api_key: Optional[str] = None, timeout: float = 10.0):
        # Remote mode: explicit `remote=` wins, else fall back to env.
        self.remote = (remote or os.environ.get("PLUTUS_REMOTE_URL") or "").rstrip("/") or None
        self.api_key = api_key or os.environ.get("PLUTUS_API_KEY")
        self.timeout = timeout
        self._last_balance: Optional[float] = None

        if self.remote:
            if not self.api_key:
                raise ValueError(
                    "remote Meter needs an api_key (or set PLUTUS_API_KEY)")
            self.cfg = config or {}
            self.conn = None
            self.org_id = None
            return

        # Local mode (unchanged).
        self.cfg = config or cfgmod.load()
        self.conn = db.connect(db_path)
        db.init_schema(self.conn)
        self.org_id = self._resolve_org(org, tier, create)

    def _resolve_org(self, org, tier, create):
        if org:
            row = (db.get_org(self.conn, org) or db.get_org_by_slug(self.conn, org))
            if row:
                return row["id"]
            for o in db.list_orgs(self.conn):
                if o["name"] == org:
                    return o["id"]
            if create:
                return db.create_org(self.conn, org, tier=tier)["id"]
            raise ValueError(f"unknown org {org!r}")
        orgs = db.list_orgs(self.conn)
        if orgs:
            return orgs[0]["id"]
        if create:
            return db.create_org(self.conn, "default", tier=tier)["id"]
        raise ValueError("no organizations exist")

    @property
    def is_remote(self) -> bool:
        return self.remote is not None

    def track(self, provider: str, *, model: Optional[str] = None,
              task_type: str = "general", workspace: Optional[str] = None,
              input_tokens: int = 0, output_tokens: int = 0,
              cache_read_tokens: int = 0, reasoning_tokens: int = 0,
              cost_usd: Optional[float] = None, source: str = "sdk"):
        """Meter one call. Returns a :class:`metering.MeterResult`."""
        if self.remote:
            event = {
                "provider": provider, "task_type": task_type, "source": source,
                "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
                "cache_read_tokens": int(cache_read_tokens),
                "reasoning_tokens": int(reasoning_tokens),
            }
            if model:
                event["model"] = model
            if workspace:
                event["workspace"] = workspace
            if cost_usd is not None:
                event["cost_usd"] = cost_usd
            return self._track_remote(event)

        return metering.record_usage(
            self.conn, self.org_id, provider=provider, model=model,
            task_type=task_type, workspace=workspace,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens, reasoning_tokens=reasoning_tokens,
            cost_usd=cost_usd, source=source,
            pricing_overrides=self.cfg.get("pricing", {}).get("overrides"),
            alert_cfg=self.cfg.get("alerts", {}),
            block_over_limit=bool(self.cfg.get("pricing", {}).get("block_over_free_limit")),
        )

    def _track_remote(self, event: dict) -> "metering.MeterResult":
        req = urllib.request.Request(
            self.remote + "/v1/usage", data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                body = {}
            if e.code == 401:
                raise PlutusAuthError("Plutus rejected the API key (HTTP 401)")
            if e.code == 402:
                # Free quota exhausted with hard-blocking on. Don't crash the
                # caller's agent — report it as a non-recorded result.
                body.setdefault("recorded", False)
                body.setdefault("over_free_limit", True)
            else:
                raise PlutusError(
                    f"Plutus ingest failed: HTTP {e.code} {body.get('error', '')}".strip())
        except urllib.error.URLError as e:
            raise PlutusError(f"could not reach Plutus at {self.remote}: {e.reason}")

        if body.get("balance_after") is not None:
            self._last_balance = float(body["balance_after"])
        return metering.MeterResult(
            event_id=body.get("event_id") or "",
            org_id=body.get("org_id") or self.remote,
            workspace_id=None, provider=event.get("provider"),
            model=event.get("model"), task_type=event.get("task_type", "general"),
            cost_usd=float(body.get("cost_usd") or 0.0),
            estimated=bool(body.get("estimated", True)),
            balance_after=float(body.get("balance_after") or 0.0), alerts=[],
            recorded=bool(body.get("recorded", True)),
            over_free_limit=bool(body.get("over_free_limit", False)),
        )

    def _local_only(self, what: str):
        if self.remote:
            raise PlutusError(
                f"{what}() isn't available in remote mode — read it from the "
                "dashboard or a track() result")

    def topup(self, amount_usd: float, reason: str = "topup"):
        self._local_only("topup")
        return db.add_ledger(self.conn, self.org_id, amount_usd, "topup", reason=reason)

    def balance(self) -> float:
        self._local_only("balance")
        return db.get_balance(self.conn, self.org_id)

    def summary(self) -> dict:
        self._local_only("summary")
        return metering.org_summary(self.conn, self.org_id)

    def close(self):
        if self.conn is not None:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
