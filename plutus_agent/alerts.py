"""Alerts — email notifications for low balance and budget caps.

Alerts are *raised* during metering (``metering._check_thresholds`` logs them to
``alerts_log``). This module is the *delivery* side: it picks up undelivered
alerts and emails them via SMTP. It is offline-safe — with alerts disabled or
SMTP unconfigured, :func:`send_pending` runs as a dry run, returning what *would*
be sent without touching the network, and leaves the alerts marked undelivered.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from . import db


def pending(conn, org_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alerts_log WHERE org_id=? AND delivered=0 ORDER BY ts",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _mark_delivered(conn, alert_id: str) -> None:
    conn.execute("UPDATE alerts_log SET delivered=1 WHERE id=?", (alert_id,))
    conn.commit()


def _build_message(alert: dict, from_addr: str, to_addrs: list[str],
                   org_name: str) -> EmailMessage:
    kind = alert["kind"].replace("_", " ").title()
    msg = EmailMessage()
    msg["Subject"] = f"[Plutus] {kind} — {org_name}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(
        f"{alert['message']}\n\n"
        f"Organization: {org_name}\n"
        f"Alert type:   {alert['kind']}\n\n"
        f"— Plutus, the billing layer for AI agents\n"
        f"  https://perseus.observer/plutus/\n"
    )
    return msg


def send_pending(conn, cfg: dict, org_id: str,
                 force: bool = False) -> dict:
    """Deliver undelivered alerts for an org. Returns a summary.

    ``force`` bypasses the ``alerts.enabled`` gate (used by ``plutus alerts
    --test``). SMTP is still required to actually send; without it this is a dry
    run.
    """
    acfg = cfg.get("alerts", {})
    org = db.get_org(conn, org_id)
    org_name = org["name"] if org else org_id
    items = pending(conn, org_id)
    if not items:
        return {"sent": 0, "dry_run": False, "pending": 0, "detail": "nothing pending"}

    enabled = acfg.get("enabled") or force
    to_addrs = acfg.get("to_addrs") or []
    smtp_host = acfg.get("smtp_host") or ""
    can_send = bool(enabled and smtp_host and to_addrs)

    if not can_send:
        reason = []
        if not enabled:
            reason.append("alerts.enabled is false")
        if not smtp_host:
            reason.append("no smtp_host")
        if not to_addrs:
            reason.append("no to_addrs")
        return {
            "sent": 0, "dry_run": True, "pending": len(items),
            "would_send": [a["message"] for a in items],
            "detail": "dry run — " + "; ".join(reason),
        }

    from_addr = acfg.get("from_addr", "plutus@perseus.observer")
    port = int(acfg.get("smtp_port", 587))
    user = acfg.get("smtp_user") or ""
    password = acfg.get("smtp_password") or ""

    sent = 0
    errors = []
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, port, timeout=20) as server:
            server.ehlo()
            if port in (587,):
                server.starttls(context=ctx)
                server.ehlo()
            if user:
                server.login(user, password)
            for a in items:
                try:
                    server.send_message(_build_message(a, from_addr, to_addrs, org_name))
                    _mark_delivered(conn, a["id"])
                    sent += 1
                except Exception as e:  # one bad alert shouldn't sink the rest
                    errors.append(f"{a['id']}: {e}")
    except Exception as e:
        return {"sent": sent, "dry_run": False, "pending": len(items) - sent,
                "error": str(e)}
    return {"sent": sent, "dry_run": False, "pending": len(items) - sent,
            "errors": errors or None}


def check_and_notify(conn, cfg: dict, org_id: Optional[str] = None) -> list[dict]:
    """Convenience for cron: deliver pending alerts for one or all orgs."""
    org_ids = [org_id] if org_id else [o["id"] for o in db.list_orgs(conn)]
    return [{"org_id": oid, **send_pending(conn, cfg, oid)} for oid in org_ids]
