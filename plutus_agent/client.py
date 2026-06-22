"""Embeddable client — the one-import way to meter agent spend from your code.

    from plutus_agent import Meter
    plutus = Meter(org="Acme Agents")          # created if it doesn't exist

    resp = call_some_llm(...)
    plutus.track(provider="anthropic", model="claude-opus-4-8",
                 task_type="code_review", workspace="ci",
                 input_tokens=resp.usage.input_tokens,
                 output_tokens=resp.usage.output_tokens)

    print(plutus.balance())     # remaining prepaid credit

Holds its own SQLite connection (open one ``Meter`` per thread). Pure offline —
no network, no Stripe — so it's safe to drop into any agent hot path.
"""
from __future__ import annotations

from typing import Optional

from . import config as cfgmod, db, metering


class Meter:
    def __init__(self, org: Optional[str] = None, *, tier: str = "free",
                 db_path: Optional[str] = None, config: Optional[dict] = None,
                 create: bool = True):
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

    def track(self, provider: str, *, model: Optional[str] = None,
              task_type: str = "general", workspace: Optional[str] = None,
              input_tokens: int = 0, output_tokens: int = 0,
              cache_read_tokens: int = 0, reasoning_tokens: int = 0,
              cost_usd: Optional[float] = None, source: str = "sdk"):
        """Meter one call. Returns a :class:`metering.MeterResult`."""
        return metering.record_usage(
            self.conn, self.org_id, provider=provider, model=model,
            task_type=task_type, workspace=workspace,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens, reasoning_tokens=reasoning_tokens,
            cost_usd=cost_usd, source=source,
            pricing_overrides=self.cfg.get("pricing", {}).get("overrides"),
            alert_cfg=self.cfg.get("alerts", {}),
        )

    def topup(self, amount_usd: float, reason: str = "topup"):
        return db.add_ledger(self.conn, self.org_id, amount_usd, "topup", reason=reason)

    def balance(self) -> float:
        return db.get_balance(self.conn, self.org_id)

    def summary(self) -> dict:
        return metering.org_summary(self.conn, self.org_id)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
