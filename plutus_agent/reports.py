"""Monthly spend reports — print-ready HTML always, PDF when reportlab is present.

``build_report`` assembles a month's spend for an org (totals, per-workspace,
per-provider, per-task-type, top models, credit movements). ``render_html``
produces a clean, printable report; ``write`` saves a ``.pdf`` (via reportlab if
installed) or a ``.html`` otherwise — so the feature works fully offline and
gains nicer output when the optional dependency is available.
"""
from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path
from typing import Optional

from . import db, metering

MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _month_bounds(year: int, month: int) -> tuple[float, float]:
    start = _dt.datetime(year, month, 1).timestamp()
    if month == 12:
        end = _dt.datetime(year + 1, 1, 1).timestamp()
    else:
        end = _dt.datetime(year, month + 1, 1).timestamp()
    return start, end


def build_report(conn, org_id: str, year: int, month: int) -> dict:
    org = db.get_org(conn, org_id)
    if org is None:
        raise ValueError(f"unknown org {org_id}")
    start, end = _month_bounds(year, month)

    def agg(dimension):
        rows = conn.execute(
            _SPEND_SQL[dimension], (org_id, start, end)
        ).fetchall()
        return [{"key": r["k"], "cost": float(r["cost"]),
                 "tokens": int(r["tok"]), "events": int(r["n"])} for r in rows]

    total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) cost, "
        "COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+reasoning_tokens),0) tok, "
        "COUNT(*) n FROM usage_events WHERE org_id=? AND ts>=? AND ts<?",
        (org_id, start, end),
    ).fetchone()

    credits = conn.execute(
        "SELECT kind, COALESCE(SUM(delta_usd),0) s, COUNT(*) n FROM credit_ledger "
        "WHERE org_id=? AND ts>=? AND ts<? GROUP BY kind",
        (org_id, start, end),
    ).fetchall()

    by_task = agg("task_type")
    for r in by_task:
        r["cost_per_event"] = round(r["cost"] / r["events"], 6) if r["events"] else 0.0

    return {
        "org": dict(org),
        "period": {"year": year, "month": month, "label": f"{MONTHS[month]} {year}"},
        "generated_at": time.time(),
        "total": {"cost": float(total["cost"]), "tokens": int(total["tok"]),
                  "events": int(total["n"])},
        "by_workspace": agg("workspace"),
        "by_provider": agg("provider"),
        "by_task_type": by_task,
        "by_model": agg("model")[:8],
        "credits": [{"kind": c["kind"], "total": float(c["s"]), "count": int(c["n"])}
                    for c in credits],
        "balance_now": db.get_balance(conn, org_id),
    }


_SPEND_SQL = {
    "provider": "SELECT provider k, COALESCE(SUM(cost_usd),0) cost, "
                "COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+reasoning_tokens),0) tok, "
                "COUNT(*) n FROM usage_events WHERE org_id=? AND ts>=? AND ts<? "
                "GROUP BY provider ORDER BY cost DESC",
    "task_type": "SELECT task_type k, COALESCE(SUM(cost_usd),0) cost, "
                 "COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+reasoning_tokens),0) tok, "
                 "COUNT(*) n FROM usage_events WHERE org_id=? AND ts>=? AND ts<? "
                 "GROUP BY task_type ORDER BY cost DESC",
    "model": "SELECT COALESCE(model,'-') k, COALESCE(SUM(cost_usd),0) cost, "
             "COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+reasoning_tokens),0) tok, "
             "COUNT(*) n FROM usage_events WHERE org_id=? AND ts>=? AND ts<? "
             "GROUP BY model ORDER BY cost DESC",
    "workspace": "SELECT COALESCE(w.name,'(none)') k, COALESCE(SUM(ue.cost_usd),0) cost, "
                 "COALESCE(SUM(ue.input_tokens+ue.output_tokens+ue.cache_read_tokens+ue.reasoning_tokens),0) tok, "
                 "COUNT(*) n FROM usage_events ue LEFT JOIN workspaces w ON w.id=ue.workspace_id "
                 "WHERE ue.org_id=? AND ue.ts>=? AND ue.ts<? GROUP BY k ORDER BY cost DESC",
}


def _usd(v):
    return f"${v:,.2f}"


def _rows(items, label):
    if not items:
        return f'<tr><td colspan="4" class="empty">No {label} this period.</td></tr>'
    out = []
    for r in items:
        cpe = f'<td class="num">{_usd(r["cost_per_event"])}</td>' if "cost_per_event" in r else ""
        out.append(
            f'<tr><td>{r["key"]}</td><td class="num">{_usd(r["cost"])}</td>'
            f'<td class="num">{r["tokens"]:,}</td><td class="num">{r["events"]:,}</td>{cpe}</tr>'
        )
    return "".join(out)


def render_html(report: dict) -> str:
    p = report["period"]
    org = report["org"]
    t = report["total"]
    gen = _dt.datetime.fromtimestamp(report["generated_at"]).strftime("%Y-%m-%d %H:%M")
    cpe_head = '<th class="num">$/task</th>'
    credit_rows = "".join(
        f'<tr><td>{c["kind"].title()}</td><td class="num">{_usd(c["total"])}</td>'
        f'<td class="num">{c["count"]}</td><td></td></tr>'
        for c in report["credits"]
    ) or '<tr><td colspan="4" class="empty">No credit movements.</td></tr>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Plutus — {org['name']} — {p['label']} spend report</title>
<style>
  @page {{ margin: 1.5cm; }}
  body {{ font: 13px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; color:#1a1626; margin:0; padding:32px; }}
  .head {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:3px solid #0c0814; padding-bottom:16px; margin-bottom:24px; }}
  .brand {{ font-size:22px; font-weight:800; letter-spacing:-.3px; }}
  .brand .amber {{ color:#d98a1f; }}
  .muted {{ color:#6b6478; font-size:12px; }}
  h1 {{ font-size:18px; margin:0 0 2px; }}
  .hero {{ display:flex; gap:28px; margin:20px 0 28px; }}
  .stat {{ }}
  .stat .v {{ font-size:28px; font-weight:800; font-variant-numeric:tabular-nums; }}
  .stat .l {{ font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#6b6478; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.6px; color:#6b6478; margin:24px 0 8px; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:8px; }}
  th,td {{ text-align:right; padding:7px 10px; border-bottom:1px solid #e7e3ef; }}
  th {{ font-size:10px; text-transform:uppercase; letter-spacing:.5px; color:#6b6478; }}
  th:first-child, td:first-child {{ text-align:left; }}
  .num {{ font-variant-numeric:tabular-nums; }}
  .empty {{ color:#9b94a8; font-style:italic; text-align:center; }}
  .foot {{ margin-top:30px; padding-top:14px; border-top:1px solid #e7e3ef; color:#9b94a8; font-size:11px; }}
</style></head><body>
<div class="head">
  <div><div class="brand"><span class="amber">◆</span> Plutus</div>
       <div class="muted">The billing layer for AI agents · Perseus Computing LLC</div></div>
  <div style="text-align:right"><h1>{org['name']}</h1>
       <div class="muted">{p['label']} · {org['tier'].title()} plan</div></div>
</div>
<div class="hero">
  <div class="stat"><div class="v">{_usd(t['cost'])}</div><div class="l">Total spend</div></div>
  <div class="stat"><div class="v">{t['tokens']:,}</div><div class="l">Tokens metered</div></div>
  <div class="stat"><div class="v">{t['events']:,}</div><div class="l">Calls</div></div>
  <div class="stat"><div class="v">{_usd(report['balance_now'])}</div><div class="l">Credit balance</div></div>
</div>

<h2>By workspace</h2>
<table><thead><tr><th>Workspace</th><th class="num">Cost</th><th class="num">Tokens</th><th class="num">Calls</th></tr></thead>
<tbody>{_rows(report['by_workspace'], 'workspace activity')}</tbody></table>

<h2>By provider</h2>
<table><thead><tr><th>Provider</th><th class="num">Cost</th><th class="num">Tokens</th><th class="num">Calls</th></tr></thead>
<tbody>{_rows(report['by_provider'], 'provider activity')}</tbody></table>

<h2>By task type</h2>
<table><thead><tr><th>Task type</th><th class="num">Cost</th><th class="num">Tokens</th><th class="num">Calls</th>{cpe_head}</tr></thead>
<tbody>{_rows(report['by_task_type'], 'task activity')}</tbody></table>

<h2>Top models</h2>
<table><thead><tr><th>Model</th><th class="num">Cost</th><th class="num">Tokens</th><th class="num">Calls</th></tr></thead>
<tbody>{_rows(report['by_model'], 'model activity')}</tbody></table>

<h2>Credit movements</h2>
<table><thead><tr><th>Kind</th><th class="num">Total</th><th class="num">Count</th><th></th></tr></thead>
<tbody>{credit_rows}</tbody></table>

<div class="foot">Generated {gen} by Plutus v{_version()}. Costs marked estimated where exact provider cost was not supplied. · perseus.observer/plutus</div>
</body></html>"""


def _version():
    from . import __version__
    return __version__


def write(report: dict, out_path: str | Path) -> Path:
    """Write the report. ``.pdf`` -> PDF if reportlab present, else sibling .html."""
    out_path = Path(out_path)
    html = render_html(report)
    if out_path.suffix.lower() == ".pdf":
        pdf = _try_pdf(report, out_path)
        if pdf:
            return pdf
        # fall back to HTML next to the requested pdf
        alt = out_path.with_suffix(".html")
        alt.write_text(html, encoding="utf-8")
        return alt
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _try_pdf(report: dict, out_path: Path) -> Optional[Path]:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError:
        return None

    styles = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#0c0814"))
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=colors.HexColor("#6b6478"))
    sec = ParagraphStyle("sec", parent=styles["Heading2"], fontSize=11,
                         textColor=colors.HexColor("#6b6478"))

    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    story = []
    org, p, t = report["org"], report["period"], report["total"]
    story.append(Paragraph(f"◆ Plutus — {org['name']}", h))
    story.append(Paragraph(f"{p['label']} · {org['tier'].title()} plan · "
                           f"The billing layer for AI agents", sub))
    story.append(Spacer(1, 16))

    hero = [["Total spend", "Tokens", "Calls", "Credit balance"],
            [_usd(t["cost"]), f"{t['tokens']:,}", f"{t['events']:,}",
             _usd(report["balance_now"])]]
    ht = Table(hero, colWidths=[1.6 * inch] * 4)
    ht.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#6b6478")),
        ("FONTSIZE", (0, 1), (-1, 1), 16),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(ht)
    story.append(Spacer(1, 16))

    def section(title, items, cpe=False):
        story.append(Paragraph(title, sec))
        head = ["Name", "Cost", "Tokens", "Calls"] + (["$/task"] if cpe else [])
        data = [head]
        for r in items or []:
            row = [str(r["key"]), _usd(r["cost"]), f"{r['tokens']:,}", f"{r['events']:,}"]
            if cpe:
                row.append(_usd(r.get("cost_per_event", 0)))
            data.append(row)
        if len(data) == 1:
            data.append(["(none)", "", "", ""] + ([""] if cpe else []))
        tbl = Table(data, hAlign="LEFT")
        tbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#6b6478")),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#cfc9da")),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#e7e3ef")),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 12))

    section("By workspace", report["by_workspace"])
    section("By provider", report["by_provider"])
    section("By task type", report["by_task_type"], cpe=True)
    section("Top models", report["by_model"])
    story.append(Paragraph(
        f"Generated by Plutus v{_version()} · perseus.observer/plutus", sub))
    doc.build(story)
    return out_path
