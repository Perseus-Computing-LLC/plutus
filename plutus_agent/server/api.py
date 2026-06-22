"""JSON API shaping for the dashboard poller and external integrations."""
from __future__ import annotations

from .. import db, metering


def default_org_id(conn) -> str | None:
    orgs = db.list_orgs(conn)
    return orgs[0]["id"] if orgs else None


def summary_json(conn, org_id: str) -> dict:
    """A flatter, poll-friendly view of an org's current state."""
    s = metering.org_summary(conn, org_id)
    return {
        "org_id": org_id,
        "org": s["org"]["name"] if s["org"] else None,
        "tier": s["tier"],
        "balance": s["balance"],
        "windows": s["windows"],
        "tracked_tokens_mtd": s["tracked_tokens_mtd"],
        "tracked_limit": s["tracked_limit"],
        "by_provider": s["by_provider"],
        "by_workspace": s["by_workspace"],
        "by_task_type": s["by_task_type"],
        "provider_health": s["provider_health"],
        "alerts": s["alerts"],
    }


def orgs_json(conn) -> list[dict]:
    return [
        {"id": o["id"], "name": o["name"], "slug": o["slug"], "tier": o["tier"],
         "balance": db.get_balance(conn, o["id"])}
        for o in db.list_orgs(conn)
    ]
