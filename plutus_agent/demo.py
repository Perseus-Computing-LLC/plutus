"""Demo data — a realistic month of agent spend, so prospects see value instantly.

``plutus serve --demo`` builds a throwaway database (in a temp file or
``~/.plutus/demo.db``) populated by :func:`seed`. The shape is deliberately
believable: a Pro-tier org with four workspaces spanning support, CI code
review, a research crew, and a data pipeline, billing across Anthropic, Google,
DeepSeek and OpenAI, with task-type mixes and prices that produce sane
cost-per-task numbers — plus one workspace pushed near its budget cap and a
credit balance low enough to show the alert path.
"""
from __future__ import annotations

import random
import time

from . import db, metering, pricing

DAY = 86400

WORKSPACES = [
    # name,                budget_usd, weight
    ("prod-support-bot",   None,  0.34),
    ("code-review-ci",     150.0, 0.30),  # has a cap -> drives budget alerts
    ("research-crew",      None,  0.22),
    ("data-pipeline",      400.0, 0.14),
]

# provider -> [(model, task_type, weight, in_range, out_range)]
WORKLOAD = {
    "anthropic": [
        ("claude-opus-4-8",            "code_review", 0.10, (4000, 18000), (800, 4000)),
        ("claude-sonnet-4-5-20250929", "chat",        0.30, (600, 4000),   (200, 1200)),
        ("claude-sonnet-4-5-20250929", "summarize",   0.12, (3000, 20000), (200, 900)),
        ("claude-haiku-4-5-20251001",  "classify",    0.10, (300, 1500),   (20, 120)),
    ],
    "google": [
        ("gemini-3.1-pro-preview", "research",  0.14, (5000, 40000), (1000, 6000)),
        ("gemini-2.5-flash",       "summarize", 0.10, (2000, 16000), (150, 700)),
        ("gemini-2.5-flash",       "chat",      0.06, (500, 3000),   (150, 900)),
    ],
    "deepseek": [
        ("deepseek-v4-pro",   "code_review", 0.04, (3000, 15000), (600, 3000)),
        ("deepseek-v4-flash", "classify",    0.03, (200, 1200),   (20, 100)),
    ],
    "openai": [
        ("gpt-5-mini", "embedding", 0.01, (500, 4000), (0, 0)),
    ],
}


def _flatten():
    rows = []
    for provider, items in WORKLOAD.items():
        for model, task, w, irange, orange in items:
            rows.append((provider, model, task, w, irange, orange))
    return rows


def seed(conn, days: int = 30, events: int = 1400, seed_value: int = 1337) -> str:
    """Populate ``conn`` with a believable month of usage. Returns the org id."""
    rng = random.Random(seed_value)
    db.init_schema(conn)

    org = db.create_org(conn, "Acme Agents", tier="pro",
                        owner_email="founder@acme.example")
    org_id = org["id"]
    db.set_stripe_customer(conn, org_id, "cus_demo_acme")

    ws_ids = {}
    for name, budget, _ in WORKSPACES:
        ws_ids[name] = db.create_workspace(conn, org_id, name, budget)["id"]

    ws_pick = [n for n, _, w in WORKSPACES for _ in range(int(w * 100))]
    work = _flatten()
    work_pick = [i for i, row in enumerate(work) for _ in range(int(row[3] * 100))]

    now = time.time()
    start = now - days * DAY
    alert_cfg = {"low_balance_usd": 25.0, "budget_warn_pct": 80.0}

    # Two prepaid top-ups during the month.
    db.add_ledger(conn, org_id, 250.0, "topup", reason="Stripe checkout (demo)",
                  stripe_ref="cs_demo_1", ts=start + 2 * DAY)
    db.add_ledger(conn, org_id, 200.0, "topup", reason="Stripe checkout (demo)",
                  stripe_ref="cs_demo_2", ts=start + 18 * DAY)

    for _ in range(events):
        # weight recent days a little heavier (ramp-up), and bias to working hours
        frac = rng.random() ** 0.7
        ts = start + frac * (now - start)
        ts += rng.uniform(-0.5, 0.5) * 3600

        provider, model, task, _w, irange, orange = work[rng.choice(work_pick)]
        wsname = rng.choice(ws_pick)
        itok = rng.randint(*irange)
        otok = rng.randint(*orange) if orange[1] else 0
        cache = int(itok * rng.uniform(0, 0.6)) if rng.random() < 0.4 else 0
        reasoning = int(otok * rng.uniform(0, 0.5)) if task in ("code_review", "research") else 0

        metering.record_usage(
            conn, org_id, provider=provider, model=model, task_type=task,
            input_tokens=itok, output_tokens=otok, cache_read_tokens=cache,
            reasoning_tokens=reasoning, workspace=wsname, source="demo",
            ts=ts, alert_cfg=alert_cfg,
        )

    bal = db.get_balance(conn, org_id)
    # Nudge the balance into low-credit territory so the alert path is visible.
    if bal > 30:
        db.add_ledger(conn, org_id, -(bal - 18.40), "debit",
                      reason="demo: trailing month spend", ts=now - 0.5 * DAY)
        metering._check_thresholds(conn, org_id, None, db.get_balance(conn, org_id),
                                   0.0, now, alert_cfg)
    return org_id
