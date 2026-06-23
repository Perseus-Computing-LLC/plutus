#!/usr/bin/env python3
"""Incrementally meter Hermes ``state.db`` sessions into a Plutus instance.

This is the cron-safe, hosted-instance counterpart to ``hermes_integration.py``
(which is a local-Meter demo). It reads *new* rows from the Hermes ``sessions``
table — the same table the credit monitor ``plutus.py`` reads — and POSTs them
to ``POST /v1/usage`` with an API key, so Hermes spend shows up live on a hosted
Plutus dashboard.

Stdlib only (sqlite3 + urllib) — no ``plutus_agent`` install needed on the
Hermes box, just python3. Progress is tracked by a ``sessions.rowid`` watermark
in a small JSON state file and advanced per successful batch, so re-runs never
double-count and a mid-run failure resumes cleanly.

    export PLUTUS_REMOTE_URL=https://plutus.perseus.observer
    export PLUTUS_API_KEY=plutus_sk_…
    python3 hermes_sync.py --dry-run     # show what would be sent
    python3 hermes_sync.py               # sync new sessions (cron this)
    python3 hermes_sync.py --reset       # forget the watermark, re-sync all

Env: PLUTUS_REMOTE_URL, PLUTUS_API_KEY (required); PLUTUS_STATE_DB (default the
Hermes path below); PLUTUS_SYNC_STATE (watermark file); PLUTUS_WORKSPACE
(default "hermes").
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DEFAULT_STATE_DB = "/opt/data/webui/minions-hermes-config/state.db"
BATCH = 500


def _session_columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}


def collect_sessions(state_db: str, last_rowid: int = 0,
                     workspace: str = "hermes") -> list[tuple[int, dict]]:
    """Return ``[(rowid, event_dict), …]`` for sessions newer than ``last_rowid``.

    Maps a Hermes session row to a ``/v1/usage`` event the same way
    ``plutus_agent.integrations.track_hermes_session`` does: prefer the exact
    ``actual_cost_usd``, fall back to ``estimated_cost_usd``. ``model`` /
    ``task_type`` are selected only if the columns exist, so this tolerates
    schema drift.
    """
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        cols = _session_columns(conn)
        model_sel = "model" if "model" in cols else "NULL"
        task_sel = "task_type" if "task_type" in cols else "NULL"
        rows = conn.execute(
            f"""SELECT rowid,
                   coalesce(nullif(billing_provider,''),'unknown') AS provider,
                   {model_sel} AS model,
                   {task_sel} AS task_type,
                   coalesce(nullif(actual_cost_usd,0), estimated_cost_usd, 0) AS cost,
                   coalesce(input_tokens,0), coalesce(output_tokens,0),
                   coalesce(cache_read_tokens,0), coalesce(reasoning_tokens,0)
                FROM sessions WHERE rowid > ? ORDER BY rowid""",
            (last_rowid,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for rowid, provider, model, task, cost, itok, otok, ctok, rtok in rows:
        ev = {
            "provider": provider,
            "task_type": task or "agent",
            "workspace": workspace,
            "source": "hermes",
            "input_tokens": int(itok), "output_tokens": int(otok),
            "cache_read_tokens": int(ctok), "reasoning_tokens": int(rtok),
        }
        if model:
            ev["model"] = model
        if cost:
            ev["cost_usd"] = float(cost)   # Hermes' own cost beats a re-estimate
        out.append((rowid, ev))
    return out


def post_events(remote: str, api_key: str, events: list[dict], timeout: float = 30.0) -> dict:
    """POST a batch of events to ``/v1/usage``; raise on a non-2xx/again-later."""
    req = urllib.request.Request(
        remote.rstrip("/") + "/v1/usage",
        data=json.dumps(events).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _load_watermark(path: str) -> int:
    try:
        return int(json.load(open(path)).get("last_rowid", 0))
    except Exception:
        return 0


def _save_watermark(path: str, rowid: int, count: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump({"last_rowid": rowid, "synced_at": time.time(), "count": count},
              open(path, "w"))


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    reset = "--reset" in argv

    remote = (os.environ.get("PLUTUS_REMOTE_URL") or "").rstrip("/")
    api_key = os.environ.get("PLUTUS_API_KEY")
    state_db = os.environ.get("PLUTUS_STATE_DB", DEFAULT_STATE_DB)
    wm_path = os.environ.get("PLUTUS_SYNC_STATE",
                             os.path.expanduser("~/.plutus/hermes_sync.json"))
    workspace = os.environ.get("PLUTUS_WORKSPACE", "hermes")

    if not remote or not api_key:
        sys.exit("plutus: set PLUTUS_REMOTE_URL and PLUTUS_API_KEY")
    if not os.path.exists(state_db):
        sys.exit(f"plutus: state.db not found: {state_db}")

    last = 0 if reset else _load_watermark(wm_path)
    pairs = collect_sessions(state_db, last, workspace)
    if not pairs:
        print(f"plutus: nothing new (watermark rowid={last})")
        return 0
    print(f"plutus: {len(pairs)} new session(s), rowid {pairs[0][0]}..{pairs[-1][0]}")

    if dry:
        print(json.dumps([e for _, e in pairs[:3]], indent=2))
        print("(dry-run — nothing sent, watermark unchanged)")
        return 0

    sent = 0
    for i in range(0, len(pairs), BATCH):
        chunk = pairs[i:i + BATCH]
        try:
            post_events(remote, api_key, [e for _, e in chunk])
        except urllib.error.HTTPError as e:
            sys.exit(f"plutus: ingest failed HTTP {e.code}: "
                     f"{e.read().decode()[:200]} (watermark at {last}, not advanced)")
        except urllib.error.URLError as e:
            sys.exit(f"plutus: could not reach {remote}: {e.reason} "
                     f"(watermark at {last}, not advanced)")
        sent += len(chunk)
        last = chunk[-1][0]
        _save_watermark(wm_path, last, sent)   # advance per batch → resumable

    print(f"plutus: metered {sent} session(s) → {remote} (watermark rowid={last})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
