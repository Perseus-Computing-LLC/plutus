"""Hermes Agent → Plutus.

The original ``plutus.py`` monitor reads Hermes' ``state.db`` after the fact.
This shows the *push* path: meter each Hermes session into Plutus as it
completes, so spend shows up live on the dashboard and depletes prepaid credit.

Drop this into a Hermes post-session hook, or batch-import an existing
``state.db`` (the loop at the bottom).
"""
import os
import sqlite3

from plutus_agent import Meter
from plutus_agent.integrations import track_hermes_session

meter = Meter(org="Hermes", tier="pro")


def on_session_complete(session: dict):
    """Call this from a Hermes post-session hook with the session row/dict."""
    res = track_hermes_session(meter, session, workspace=session.get("workspace", "hermes"))
    if res.alerts:
        for a in res.alerts:
            print(f"[plutus] ALERT {a['kind']}: {a['message']}")
    return res


def backfill_from_state_db(state_db: str):
    """One-time import of historical Hermes sessions into Plutus."""
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT billing_provider, model, started_at, actual_cost_usd, "
        "estimated_cost_usd, input_tokens, output_tokens, cache_read_tokens, "
        "reasoning_tokens FROM sessions"
    ).fetchall()
    conn.close()
    n = 0
    for r in rows:
        track_hermes_session(meter, dict(r))
        n += 1
    print(f"imported {n} sessions → balance ${meter.balance():.2f}")


if __name__ == "__main__":
    state_db = os.environ.get(
        "PLUTUS_STATE_DB",
        "/opt/data/webui/minions-hermes-config/state.db")
    if os.path.exists(state_db):
        backfill_from_state_db(state_db)
    else:
        # demo a single synthetic session
        on_session_complete({
            "billing_provider": "anthropic", "model": "claude-opus-4-8",
            "task_type": "code_review", "actual_cost_usd": 0.142,
            "input_tokens": 9100, "output_tokens": 2300,
        })
        print(f"balance ${meter.balance():.4f}")
    meter.close()
