"""Metering — the revenue-bearing core.

``record_usage`` is the single entry point every integration funnels through:

    plutus_agent.metering.record_usage(conn, org_id, provider="anthropic",
        model="claude-opus-4-8", task_type="code_review",
        input_tokens=1200, output_tokens=800, workspace="prod")

It (1) resolves/creates the workspace, (2) prices the event (exact cost if given,
else estimated from token counts), (3) writes an immutable ``usage_events`` row,
(4) **depletes prepaid credit** via the append-only ledger, and (5) runs budget /
low-balance checks, queueing alerts when thresholds trip.

Everything else here is read-side aggregation for the dashboard, reports, and
the metered free-tier limit. All windows are computed relative to ``now`` so the
numbers match the original monitor's today / 7d / 30d framing, plus
month-to-date for billing periods.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Optional

from . import db, pricing

DAY = 86400


@dataclass
class MeterResult:
    event_id: str
    org_id: str
    workspace_id: Optional[str]
    provider: str
    model: Optional[str]
    task_type: str
    cost_usd: float
    estimated: bool
    balance_after: float
    alerts: list  # list of dicts {kind, message}
    recorded: bool = True        # False when a hard free-tier cap dropped the event
    over_free_limit: bool = False  # org is on a limited tier and past its monthly quota
    over_balance: bool = False  # Fix #28: org hit prepaid credit hard-stop


def _resolve_workspace(conn, org_id: str, workspace: Optional[str],
                       commit: bool = True) -> Optional[str]:
    """Accept a workspace id, slug, or name; create-by-name if not found.

    Honors the org tier's workspace cap: once an org is at its limit (Free = 1),
    a usage event tagged with a *new* workspace folds into the org's earliest
    existing workspace instead of creating another. Tracking never breaks; the
    cap just stops the workspace count from growing.

    ``commit`` is forwarded to :func:`db.create_workspace` so that, inside a
    batch/transaction (``record_usage(commit=False)`` under ``db.immediate``),
    auto-creating a workspace does not commit the open transaction early.
    """
    if not workspace:
        return None
    ws = db.get_workspace(conn, workspace)
    if ws and ws["org_id"] == org_id:
        return ws["id"]
    row = conn.execute(
        "SELECT * FROM workspaces WHERE org_id=? AND (slug=? OR name=?)",
        (org_id, workspace, workspace),
    ).fetchone()
    if row:
        return row["id"]

    existing = db.list_workspaces(conn, org_id)
    org = db.get_org(conn, org_id)
    cap = pricing.tier(org["tier"]).workspaces if org else None
    if cap is not None and len(existing) >= cap:
        return existing[0]["id"] if existing else None
    return db.create_workspace(conn, org_id, workspace, commit=commit)["id"]


def record_usage(conn, org_id: str, provider: str,
                 input_tokens: int = 0, output_tokens: int = 0,
                 cache_read_tokens: int = 0, reasoning_tokens: int = 0,
                 model: Optional[str] = None, task_type: str = "general",
                 workspace: Optional[str] = None,
                 cost_usd: Optional[float] = None, source: str = "api",
                 pricing_overrides: Optional[dict] = None,
                 ts: Optional[float] = None,
                 alert_cfg: Optional[dict] = None,
                 block_over_limit: bool = False,
                 block_over_balance: bool = False,
                 commit: bool = True) -> MeterResult:
    """Meter one LLM/agent call. Returns a :class:`MeterResult`.

    Free-tier quota: an org on a limited tier is flagged ``over_free_limit``
    once its month-to-date tracked tokens reach the tier cap. With
    ``block_over_limit`` the event past the cap is *not* recorded (``recorded``
    is False) — otherwise it is still recorded so no billing data is lost.
    
    Prepaid credit hard-stop (fix #28): with ``block_over_balance`` on, if the
    org has ever had credit and the event would push balance negative, it is
    rejected (not recorded).
    """
    ts = ts if ts is not None else time.time()

    org = db.get_org(conn, org_id)
    limit = pricing.tier(org["tier"]).tracked_tokens_month if org else None
    event_tokens = (int(input_tokens) + int(output_tokens)
                    + int(cache_read_tokens) + int(reasoning_tokens))
    tracked_before = tracked_tokens_mtd(conn, org_id, ts) if limit is not None else 0
    if limit is not None and block_over_limit and tracked_before >= limit:
        return MeterResult(
            event_id="", org_id=org_id, workspace_id=None,
            provider=provider, model=model, task_type=task_type,
            cost_usd=0.0, estimated=cost_usd is None,
            balance_after=db.get_balance(conn, org_id), alerts=[],
            recorded=False, over_free_limit=True,
        )

    workspace_id = _resolve_workspace(conn, org_id, workspace, commit=commit)

    estimated = cost_usd is None
    if estimated:
        cost_usd = pricing.estimate_cost(
            provider, model, input_tokens, output_tokens,
            cache_read_tokens, reasoning_tokens, pricing_overrides,
        )
    cost_usd = round(float(cost_usd), 6)

    # Fix #61: never let a negative cost through the debit hot path. A negative
    # cost_usd would be passed to db.add_ledger(..., -cost_usd, "debit") where
    # -(-x) becomes a *positive* delta — minting prepaid credit out of thin air —
    # and would also defeat the hard-stop below (balance - cost_usd can't go
    # negative when cost_usd < 0). Genuine corrections/credits must go through an
    # explicit adjust/grant/refund ledger path, never metering. This is the
    # authoritative guard; the /v1/usage boundary also rejects negatives with a
    # 400 so HTTP callers get a clean error before ever reaching here.
    if cost_usd < 0:
        raise ValueError(
            f"cost_usd must be non-negative, got {cost_usd}; credits/refunds "
            "must go through the adjust/grant/refund ledger path, not metering"
        )

    # Fix #28: prepaid credit hard-stop. Skipped for orgs explicitly flagged
    # allow_negative_balance (trusted/internal track-only mode) so they keep
    # full tracking even past zero.
    balance = db.get_balance(conn, org_id)
    exempt = bool(org and org["allow_negative_balance"])
    if block_over_balance and not exempt:
        # Check if org has ever had credit
        had_credit = conn.execute(
            "SELECT 1 FROM credit_ledger WHERE org_id=? AND kind IN ('topup','grant') LIMIT 1",
            (org_id,),
        ).fetchone()
        if had_credit and balance - cost_usd < 0:
            return MeterResult(
                event_id="", org_id=org_id, workspace_id=workspace_id,
                provider=provider, model=model, task_type=task_type,
                cost_usd=cost_usd, estimated=estimated,
                balance_after=balance, alerts=[],
                recorded=False, over_free_limit=False, over_balance=True,
            )

    eid = db.new_id("evt")
    conn.execute(
        "INSERT INTO usage_events(id,org_id,workspace_id,provider,model,task_type,"
        "input_tokens,output_tokens,cache_read_tokens,reasoning_tokens,cost_micros,"
        "estimated,source,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, org_id, workspace_id, provider, model, task_type,
         int(input_tokens), int(output_tokens), int(cache_read_tokens),
         int(reasoning_tokens), db.usd_to_micros(cost_usd), int(estimated), source, ts),
    )

    # Deplete prepaid credit (only when there's credit to deplete; orgs on the
    # free tier with no balance still get full usage tracking, just no debit).
    if balance > 0 or cost_usd > 0:
        row = db.add_ledger(conn, org_id, -cost_usd, "debit",
                            reason=f"{provider}/{model or '-'} {task_type}",
                            stripe_ref=eid, ts=ts, commit=False)
        balance_after = float(row["balance_after"])
    else:
        balance_after = balance
    if commit:
        conn.commit()

    alerts = _check_thresholds(conn, org_id, workspace_id, balance_after,
                               cost_usd, ts, alert_cfg or {}, commit=commit)

    over = limit is not None and (tracked_before + event_tokens) >= limit
    return MeterResult(
        event_id=eid, org_id=org_id, workspace_id=workspace_id,
        provider=provider, model=model, task_type=task_type,
        cost_usd=cost_usd, estimated=estimated,
        balance_after=balance_after, alerts=alerts,
        recorded=True, over_free_limit=over,
    )


def _check_thresholds(conn, org_id, workspace_id, balance_after, cost_usd,
                      ts, alert_cfg, commit: bool = True) -> list:
    """Queue alerts when credit runs low or a workspace nears/exceeds its cap.

    Alerts are *logged* here (so the dashboard can show them and a sender can
    pick them up); actual email delivery is the alerts module's job. Returns the
    list of alerts raised by this event.
    """
    raised = []
    low = float(alert_cfg.get("low_balance_usd", 10.0))
    warn_pct = float(alert_cfg.get("budget_warn_pct", 80.0))

    # low balance — only meaningful once the org has ever had credit
    had_credit = conn.execute(
        "SELECT 1 FROM credit_ledger WHERE org_id=? AND kind IN ('topup','grant') LIMIT 1",
        (org_id,),
    ).fetchone()
    if had_credit and 0 < balance_after <= low:
        msg = f"Low credit balance: ${balance_after:,.2f} (threshold ${low:,.2f})"
        if not _alerted_recently(conn, org_id, "low_balance", None, ts):
            db.log_alert(conn, org_id, "low_balance", msg, commit=commit)
            raised.append({"kind": "low_balance", "message": msg})
    elif had_credit and balance_after <= 0:
        msg = f"Credit exhausted: balance ${balance_after:,.2f}"
        if not _alerted_recently(conn, org_id, "balance_exhausted", None, ts):
            db.log_alert(conn, org_id, "balance_exhausted", msg, commit=commit)
            raised.append({"kind": "balance_exhausted", "message": msg})

    # workspace monthly budget
    if workspace_id:
        ws = db.get_workspace(conn, workspace_id)
        cap = ws["monthly_budget_usd"] if ws else None
        if cap and cap > 0:
            spent = workspace_mtd_spend(conn, workspace_id, ts)
            pct = spent / cap * 100.0
            if spent >= cap:
                msg = (f"Workspace '{ws['name']}' over budget: "
                       f"${spent:,.2f} / ${cap:,.2f} ({pct:.0f}%)")
                kind = "budget_cap"
            elif pct >= warn_pct:
                msg = (f"Workspace '{ws['name']}' at {pct:.0f}% of budget: "
                       f"${spent:,.2f} / ${cap:,.2f}")
                kind = "budget_warn"
            else:
                return raised
            if not _alerted_recently(conn, org_id, kind, workspace_id, ts):
                db.log_alert(conn, org_id, kind, msg, workspace_id=workspace_id, commit=commit)
                raised.append({"kind": kind, "message": msg})
    return raised


def _alerted_recently(conn, org_id, kind, workspace_id, ts, within=DAY) -> bool:
    """De-dupe: don't re-raise the same alert more than once per day."""
    row = conn.execute(
        "SELECT ts FROM alerts_log WHERE org_id=? AND kind=? AND "
        "COALESCE(workspace_id,'')=COALESCE(?,'') ORDER BY ts DESC LIMIT 1",
        (org_id, kind, workspace_id),
    ).fetchone()
    return bool(row and (ts - row["ts"]) < within)


# ------------------------------------------------------------ aggregation ----
def _month_floor(ts: float) -> float:
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ts)
    return _dt.datetime(d.year, d.month, 1).timestamp()


def workspace_mtd_spend(conn, workspace_id: str, now: Optional[float] = None) -> float:
    now = now if now is not None else time.time()
    floor = _month_floor(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_micros),0) s FROM usage_events WHERE workspace_id=? AND ts>=?",
        (workspace_id, floor),
    ).fetchone()
    return db.micros_to_usd(int(row["s"]))


def org_spend_windows(conn, org_id: str, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    windows = {"today": now - DAY, "7d": now - 7 * DAY, "30d": now - 30 * DAY,
               "mtd": _month_floor(now), "all": 0}
    out = {}
    for name, floor in windows.items():
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_micros),0) cost, COALESCE(SUM(input_tokens+output_tokens"
            "+cache_read_tokens+reasoning_tokens),0) tok, COUNT(*) n "
            "FROM usage_events WHERE org_id=? AND ts>=?",
            (org_id, floor),
        ).fetchone()
        out[name] = {"cost": db.micros_to_usd(int(row["cost"])), "tokens": int(row["tok"]),
                     "events": int(row["n"])}
    return out


def spend_by(conn, org_id: str, dimension: str, since: float = 0,
             now: Optional[float] = None) -> list[dict]:
    """Group spend by 'provider', 'workspace', 'task_type', or 'model'."""
    now = now if now is not None else time.time()
    col = {
        "provider": "ue.provider",
        "task_type": "ue.task_type",
        "model": "COALESCE(ue.model,'-')",
        "workspace": "COALESCE(w.name, '(none)')",
    }[dimension]
    join = "LEFT JOIN workspaces w ON w.id = ue.workspace_id" if dimension == "workspace" else ""
    rows = conn.execute(
        f"SELECT {col} AS k, COALESCE(SUM(ue.cost_micros),0) cost, "
        f"COALESCE(SUM(ue.input_tokens+ue.output_tokens+ue.cache_read_tokens+ue.reasoning_tokens),0) tok, "
        f"COUNT(*) n FROM usage_events ue {join} "
        f"WHERE ue.org_id=? AND ue.ts>=? GROUP BY k ORDER BY cost DESC",
        (org_id, since),
    ).fetchall()
    return [{"key": r["k"], "cost": db.micros_to_usd(int(r["cost"])), "tokens": int(r["tok"]),
             "events": int(r["n"])} for r in rows]


def provider_health(conn, org_id: str, now: Optional[float] = None) -> list[dict]:
    """Per-provider recency + burn, a proxy for "health" on the dashboard.

    healthy = activity in the last 24h; idle = older; the trailing 7-day burn is
    the same $/day figure the monitor reports.
    """
    now = now if now is not None else time.time()
    rows = conn.execute(
        "SELECT provider, MAX(ts) last_ts, "
        "COALESCE(SUM(CASE WHEN ts>=? THEN cost_micros ELSE 0 END),0) c7, "
        "COALESCE(SUM(cost_micros),0) all_cost, COUNT(*) n "
        "FROM usage_events WHERE org_id=? GROUP BY provider ORDER BY all_cost DESC",
        (now - 7 * DAY, org_id),
    ).fetchall()
    out = []
    for r in rows:
        age = now - (r["last_ts"] or 0)
        status = "healthy" if age < DAY else ("idle" if age < 7 * DAY else "stale")
        out.append({
            "provider": r["provider"],
            "last_ts": r["last_ts"],
            "burn_per_day": round(db.micros_to_usd(int(r["c7"])) / 7.0, 4),
            "all_cost": db.micros_to_usd(int(r["all_cost"])),
            "events": int(r["n"]),
            "status": status,
        })
    return out


def cost_per_task(conn, org_id: str, now: Optional[float] = None) -> list[dict]:
    """Cost-per-task-type: total cost / event count, the ROI lens."""
    rows = spend_by(conn, org_id, "task_type", since=0, now=now)
    for r in rows:
        r["cost_per_event"] = round(r["cost"] / r["events"], 6) if r["events"] else 0.0
    return rows


def tracked_tokens_mtd(conn, org_id: str, now: Optional[float] = None) -> int:
    """Tokens tracked month-to-date — drives the free-tier 10K limit."""
    now = now if now is not None else time.time()
    floor = _month_floor(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+reasoning_tokens),0) t "
        "FROM usage_events WHERE org_id=? AND ts>=?",
        (org_id, floor),
    ).fetchone()
    return int(row["t"])


def tier_status(conn, org_id: str, now: Optional[float] = None) -> dict:
    """Plan limits vs. current usage — the single source of truth for the
    free-tier meter and the in-app upgrade nudge.

    ``near`` trips at 75% of the token quota; ``over`` once it's reached. Both
    are False on unlimited tiers (Pro / Enterprise).
    """
    org = db.get_org(conn, org_id)
    t = pricing.tier(org["tier"]) if org else pricing.tier("free")
    tokens = tracked_tokens_mtd(conn, org_id, now)
    limit = t.tracked_tokens_month
    pct = (tokens / limit * 100.0) if limit else None
    ws_used = len(db.list_workspaces(conn, org_id))
    return {
        "tier": t.key,
        "tier_name": t.name,
        "is_free": t.key == "free",
        "tracked_tokens": tokens,
        "tracked_limit": limit,
        "tracked_pct": pct,
        "near_limit": bool(limit and pct is not None and pct >= 75.0),
        "over_limit": bool(limit and tokens >= limit),
        "workspaces_used": ws_used,
        "workspaces_limit": t.workspaces,
        "workspaces_over": bool(t.workspaces is not None and ws_used >= t.workspaces),
    }


def recent_events(conn, org_id: str, limit: int = 25) -> list[dict]:
    rows = conn.execute(
        "SELECT ue.*, w.name AS workspace_name FROM usage_events ue "
        "LEFT JOIN workspaces w ON w.id=ue.workspace_id "
        "WHERE ue.org_id=? ORDER BY ue.ts DESC LIMIT ?",
        (org_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["cost_usd"] = db.micros_to_usd(int(d.get("cost_micros", 0) or 0))
        out.append(d)
    return out


def org_summary(conn, org_id: str, now: Optional[float] = None) -> dict:
    """One call that assembles everything the dashboard needs for an org."""
    org = db.get_org(conn, org_id)
    t = pricing.tier(org["tier"]) if org else pricing.tier("free")
    windows = org_spend_windows(conn, org_id, now)
    tracked = tracked_tokens_mtd(conn, org_id, now)
    limit = t.tracked_tokens_month
    return {
        "org": dict(org) if org else None,
        "tier": {"key": t.key, "name": t.name, "price": t.price_usd_month},
        "balance": db.get_balance(conn, org_id),
        "windows": windows,
        "tracked_tokens_mtd": tracked,
        "tracked_limit": limit,
        "tracked_pct": (tracked / limit * 100.0) if limit else None,
        "tier_status": tier_status(conn, org_id, now),
        "by_provider": spend_by(conn, org_id, "provider", now=now),
        "by_workspace": spend_by(conn, org_id, "workspace", now=now),
        "by_task_type": cost_per_task(conn, org_id, now=now),
        "provider_health": provider_health(conn, org_id, now=now),
        "workspaces": [dict(w) for w in db.list_workspaces(conn, org_id)],
        "recent_events": recent_events(conn, org_id, 12),
        "alerts": [dict(a) for a in db.recent_alerts(conn, org_id, 6)],
    }
