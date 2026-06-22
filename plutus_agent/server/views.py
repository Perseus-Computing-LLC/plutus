"""Dashboard rendering — dark theme anchored on perseus.observer ``#0c0814``.

Pure functions: ``(summary dict) -> HTML string``. No framework, no external
assets (CSP-safe, works offline). A tiny inline poller re-fetches
``/api/summary`` every few seconds and live-updates the headline numbers; the
page also degrades to a periodic full reload if JS is disabled.
"""
from __future__ import annotations

import datetime as _dt
import html

# Brand — #0c0814 base + Perseus deck accents (amber numbers, green positive,
# coral problem), JetBrains-Mono-style numerals.
CSS = """
:root{
  --bg:#0c0814; --bg2:#120c1d; --panel:#161020; --panel2:#1d1529;
  --line:#2a2238; --line2:#372c4a;
  --txt:#ece7f5; --dim:#9b91ad; --faint:#6f6682;
  --amber:#f5b63f; --amber-dim:#7a6228;
  --green:#54d38a; --green-dim:#2f6b4c;
  --coral:#f26a52; --coral-dim:#7a3a30;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:
  radial-gradient(1200px 600px at 80% -10%, #1a1030 0%, transparent 60%),
  radial-gradient(900px 500px at -10% 10%, #12182e 0%, transparent 55%),
  var(--bg);
  color:var(--txt);font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100vh}
a{color:var(--amber);text-decoration:none}
.wrap{max-width:1180px;margin:0 auto;padding:26px 22px 64px}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:8px}
.brand{display:flex;align-items:center;gap:11px}
.logo{font-size:22px;color:var(--amber)}
.brand h1{font-size:19px;margin:0;font-weight:800;letter-spacing:-.3px}
.brand .tag{color:var(--dim);font-size:12px;margin-top:1px}
.pill{font-size:11px;padding:3px 9px;border-radius:20px;border:1px solid var(--line2);color:var(--dim)}
.pill.pro{color:var(--amber);border-color:var(--amber-dim);background:rgba(245,182,63,.08)}
.pill.live{color:var(--green);border-color:var(--green-dim);background:rgba(84,211,138,.08)}
.pill.demo{color:var(--coral);border-color:var(--coral-dim);background:rgba(242,106,82,.08)}
.orgsel{background:var(--panel);color:var(--txt);border:1px solid var(--line2);border-radius:8px;padding:6px 10px;font-size:13px}
.banner{margin:14px 0;border-radius:12px;border:1px solid var(--coral-dim);background:rgba(242,106,82,.08);
  padding:11px 15px;color:#ffd9cf;font-size:13px;display:flex;gap:10px;align-items:flex-start}
.banner .x{color:var(--coral);font-weight:700}
.grid{display:grid;gap:16px}
.cards{grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin:18px 0}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:14px;padding:16px 17px}
.card .l{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--faint)}
.card .v{font-size:27px;font-weight:800;font-family:var(--mono);margin-top:5px;letter-spacing:-.5px}
.card .v.amber{color:var(--amber)} .card .v.green{color:var(--green)} .card .v.coral{color:var(--coral)}
.card .s{font-size:12px;color:var(--dim);margin-top:3px}
.cols{grid-template-columns:1fr 1fr}
@media(max-width:860px){.cols{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:4px 0 6px;overflow:hidden}
.panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);
  margin:0;padding:14px 18px 10px;display:flex;justify-content:space-between;align-items:center}
.panel h2 .hint{color:var(--faint);font-weight:400;text-transform:none;letter-spacing:0}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 18px;text-align:right;border-top:1px solid var(--line)}
th{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--faint);font-weight:600}
th:first-child,td:first-child{text-align:left}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.name{font-weight:600}
.bar{height:6px;border-radius:4px;background:var(--line2);overflow:hidden;margin-top:5px}
.bar > i{display:block;height:100%;background:var(--amber)}
.bar.warn > i{background:var(--coral)}
.bar.ok > i{background:var(--green)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}
.dot.healthy{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot.idle{background:var(--amber)} .dot.stale{background:var(--faint)}
.muted{color:var(--dim)} .empty{color:var(--faint);padding:18px;text-align:center;font-style:italic}
.feed{max-height:340px;overflow:auto}
.feed .row{display:flex;justify-content:space-between;gap:12px;padding:8px 18px;border-top:1px solid var(--line);font-size:13px}
.feed .row:first-child{border-top:none}
.feed .meta{color:var(--dim);font-size:12px}
.tag2{font-size:10px;padding:1px 6px;border-radius:5px;background:var(--bg2);border:1px solid var(--line2);color:var(--dim);margin-left:6px}
.billing{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:14px 18px}
.btn{background:var(--amber);color:#1a1206;border:none;border-radius:9px;padding:9px 15px;font-weight:700;font-size:13px;cursor:pointer}
.btn.ghost{background:transparent;color:var(--txt);border:1px solid var(--line2)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.amt{width:96px;background:var(--bg2);border:1px solid var(--line2);color:var(--txt);border-radius:9px;padding:9px 11px;font-family:var(--mono)}
.foot{margin-top:30px;color:var(--faint);font-size:12px;text-align:center}
.spark{display:flex;gap:2px;align-items:flex-end;height:26px}
.spark i{flex:1;background:var(--amber-dim);border-radius:1px;min-height:2px}
"""

POLLER = """
async function poll(){
  try{
    const u=new URL(location.href); const org=u.searchParams.get('org')||'';
    const r=await fetch('/api/summary'+(org?('?org='+encodeURIComponent(org)):''));
    if(!r.ok)return; const d=await r.json();
    const set=(id,v)=>{const e=document.getElementById(id); if(e)e.textContent=v;};
    const usd=v=>'$'+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    set('v-balance',usd(d.balance));
    set('v-today',usd(d.windows.today.cost));
    set('v-mtd',usd(d.windows.mtd.cost));
    set('v-events',Number(d.windows.mtd.events).toLocaleString());
    document.getElementById('pulse')?.classList.remove('off');
    setTimeout(()=>document.getElementById('pulse')?.classList.add('off'),600);
  }catch(e){}
}
setInterval(poll,5000);
"""


FAVICON = ("<link rel='icon' href=\"data:image/svg+xml,"
           "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='6' fill='%230c0814'/%3E"
           "%3Ctext x='16' y='23' font-size='20' text-anchor='middle' fill='%23f5b63f'%3E"
           "%E2%97%86%3C/text%3E%3C/svg%3E\">")


def _usd(v):
    return "—" if v is None else f"${v:,.2f}"


def _ago(ts):
    if not ts:
        return "—"
    import time
    s = max(0, time.time() - ts)
    if s < 60:
        return f"{int(s)}s ago"
    if s < 3600:
        return f"{int(s/60)}m ago"
    if s < 86400:
        return f"{int(s/3600)}h ago"
    return f"{int(s/86400)}d ago"


def _e(s):
    return html.escape(str(s))


def render_dashboard(summary: dict, *, orgs: list, cfg: dict,
                     stripe_status: dict, demo: bool = False,
                     runway: dict | None = None) -> str:
    org = summary["org"]
    tier = summary["tier"]
    w = summary["windows"]
    bal = summary["balance"]
    low = bal is not None and bal <= float(cfg.get("alerts", {}).get("low_balance_usd", 10.0))

    # alerts banner
    banner = ""
    if summary["alerts"]:
        items = "".join(f"<div><span class='x'>▲</span> {_e(a['message'])}</div>"
                        for a in summary["alerts"][:3])
        banner = f"<div class='banner'><div>{items}</div></div>"

    # org selector
    opts = "".join(
        f"<option value='{_e(o['id'])}' {'selected' if o['id']==org['id'] else ''}>{_e(o['name'])}</option>"
        for o in orgs
    )
    orgsel = (f"<select class='orgsel' onchange=\"location.href='/?org='+this.value\">{opts}</select>"
              if len(orgs) > 1 else "")

    # tracked-tokens meter (free tier limit)
    tracked, limit = summary["tracked_tokens_mtd"], summary["tracked_limit"]
    if limit:
        pct = min(100.0, summary["tracked_pct"] or 0)
        cls = "warn" if pct >= 90 else ("ok" if pct < 70 else "")
        meter = (f"<div class='card'><div class='l'>Tracked tokens · this month</div>"
                 f"<div class='v {'coral' if pct>=90 else 'amber'}'>{tracked:,}</div>"
                 f"<div class='s'>of {limit:,} ({pct:.0f}%) — {tier['name']} plan</div>"
                 f"<div class='bar {cls}'><i style='width:{pct:.0f}%'></i></div></div>")
    else:
        meter = (f"<div class='card'><div class='l'>Tracked tokens · this month</div>"
                 f"<div class='v amber'>{tracked:,}</div>"
                 f"<div class='s'>unlimited — {tier['name']} plan</div></div>")

    cards = f"""
    <div class="grid cards">
      <div class="card"><div class="l">Credit balance</div>
        <div class="v {'coral' if low else 'green'}" id="v-balance">{_usd(bal)}</div>
        <div class="s">{'⚠ low balance' if low else 'prepaid · auto-depletes'}</div></div>
      <div class="card"><div class="l">Spend today</div>
        <div class="v amber" id="v-today">{_usd(w['today']['cost'])}</div>
        <div class="s">{w['today']['events']:,} calls</div></div>
      <div class="card"><div class="l">Month to date</div>
        <div class="v" id="v-mtd">{_usd(w['mtd']['cost'])}</div>
        <div class="s"><span id="v-events">{w['mtd']['events']:,}</span> calls · 7d {_usd(w['7d']['cost'])}</div></div>
      {meter}
    </div>"""

    # workspaces with budget bars
    ws_rows = []
    ws_spend = {x["key"]: x for x in summary["by_workspace"]}
    for ws in summary["workspaces"]:
        sp = ws_spend.get(ws["name"], {"cost": 0, "events": 0, "tokens": 0})
        cap = ws["monthly_budget_usd"]
        if cap:
            pct = min(100.0, sp["cost"] / cap * 100.0) if cap else 0
            cls = "warn" if pct >= 80 else "ok"
            budget = (f"<div class='bar {cls}'><i style='width:{pct:.0f}%'></i></div>"
                      f"<div class='muted' style='font-size:11px;margin-top:3px'>{_usd(sp['cost'])} / {_usd(cap)} ({pct:.0f}%)</div>")
        else:
            budget = "<div class='muted' style='font-size:11px;margin-top:3px'>no cap</div>"
        ws_rows.append(
            f"<tr><td class='name'>{_e(ws['name'])}{budget}</td>"
            f"<td class='num'>{_usd(sp['cost'])}</td>"
            f"<td class='num'>{sp['tokens']:,}</td>"
            f"<td class='num'>{sp['events']:,}</td></tr>")
    ws_table = ("".join(ws_rows) or "<tr><td colspan=4 class='empty'>No workspaces yet.</td></tr>")

    # providers + health
    prov_health = {p["provider"]: p for p in summary["provider_health"]}
    maxp = max([p["cost"] for p in summary["by_provider"]] + [1e-9])
    prov_rows = []
    for p in summary["by_provider"]:
        h = prov_health.get(p["key"], {"status": "stale", "burn_per_day": 0, "last_ts": None})
        barw = p["cost"] / maxp * 100.0
        prov_rows.append(
            f"<tr><td class='name'><span class='dot {h['status']}'></span>{_e(p['key'])}"
            f"<div class='bar'><i style='width:{barw:.0f}%'></i></div></td>"
            f"<td class='num'>{_usd(p['cost'])}</td>"
            f"<td class='num'>{_usd(h['burn_per_day'])}</td>"
            f"<td class='num muted'>{_ago(h['last_ts'])}</td></tr>")
    prov_table = ("".join(prov_rows) or "<tr><td colspan=4 class='empty'>No usage yet.</td></tr>")

    # cost per task type
    task_rows = []
    for tt in summary["by_task_type"]:
        task_rows.append(
            f"<tr><td class='name'>{_e(tt['key'])}</td>"
            f"<td class='num'>{_usd(tt['cost'])}</td>"
            f"<td class='num'>{tt['events']:,}</td>"
            f"<td class='num amber'>{_usd(tt.get('cost_per_event',0))}</td></tr>")
    task_table = ("".join(task_rows) or "<tr><td colspan=4 class='empty'>No tasks yet.</td></tr>")

    # recent feed
    feed = []
    for ev in summary["recent_events"]:
        est = "<span class='tag2'>est</span>" if ev.get("estimated") else ""
        feed.append(
            f"<div class='row'><div><span class='name'>{_e(ev['provider'])}</span>"
            f"<span class='tag2'>{_e(ev.get('task_type','-'))}</span>"
            f"<div class='meta'>{_e(ev.get('workspace_name') or '—')} · {_e(ev.get('model') or '-')}</div></div>"
            f"<div style='text-align:right'><span class='num amber'>{_usd(ev['cost_usd'])}</span>{est}"
            f"<div class='meta'>{_ago(ev['ts'])}</div></div></div>")
    feed_html = ("".join(feed) or "<div class='empty'>No calls metered yet.</div>")

    # billing panel
    can_checkout = stripe_status["available"]
    pro_disabled = "" if (can_checkout and stripe_status["has_pro_price"]) else "disabled"
    sb = stripe_status["mode"]
    if can_checkout:
        billing = f"""
        <form class="billing" method="post" action="/billing/checkout/credit">
          <input type="hidden" name="org" value="{_e(org['id'])}">
          <span class="muted">Buy prepaid credit:</span>
          <input class="amt" type="number" name="amount" value="50" min="5" step="5">
          <button class="btn" type="submit">Top up →</button>
          <button class="btn ghost" type="submit" formaction="/billing/checkout/pro" {pro_disabled}>Upgrade to Pro · $20/mo</button>
          <button class="btn ghost" type="submit" formaction="/billing/portal">Manage billing</button>
          <span class="muted" style="margin-left:auto">Stripe: {_e(sb)}</span>
        </form>"""
    else:
        billing = f"""
        <div class="billing">
          <span class="muted">Stripe is {_e(sb)}. Credit top-ups & Pro checkout activate once you set
          <span class="num">STRIPE_SECRET_KEY</span>. Everything else runs fully offline.</span>
        </div>"""

    # optional live runway panel (from the monitor bridge)
    runway_panel = ""
    if runway and runway.get("providers"):
        rr = []
        for p in runway["providers"]:
            bal_s = _usd(p.get("balance")) if p.get("balance") is not None else _usd(p.get("remaining"))
            days = p.get("days_left")
            days_s = "∞" if days is None else f"{days:.0f}d"
            src = "live" if p.get("source") == "live" else "ledger"
            rr.append(f"<tr><td class='name'>{_e(p['provider'])}<span class='tag2'>{src}</span></td>"
                      f"<td class='num green'>{bal_s}</td>"
                      f"<td class='num'>{_usd(p.get('burn_per_day'))}</td>"
                      f"<td class='num'>{days_s}</td></tr>")
        runway_panel = f"""
        <div class="panel">
          <h2>Provider runway <span class="hint">live, via plutus.py monitor</span></h2>
          <table><thead><tr><th>Provider</th><th>Balance</th><th>$/day</th><th>Runway</th></tr></thead>
          <tbody>{''.join(rr)}</tbody></table>
        </div>"""

    gen = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    from .. import __version__, __tagline__
    badges = (f"<span class='pill {tier['key']}'>{_e(tier['name'])} plan</span>"
              + ("<span class='pill demo'>DEMO DATA</span>" if demo else "")
              + "<span class='pill live' id='pulse'>● live</span>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plutus — {_e(org['name'])} · spend dashboard</title>{FAVICON}
<style>{CSS}
#pulse{{transition:opacity .4s}}#pulse.off{{opacity:.4}}</style>
</head><body><div class="wrap">
  <div class="top">
    <div class="brand"><div class="logo">◆</div>
      <div><h1>Plutus</h1><div class="tag">{_e(__tagline__)}</div></div></div>
    <div style="display:flex;gap:8px;align-items:center">{orgsel}{badges}</div>
  </div>
  {banner}
  {cards}
  <div class="grid cols">
    <div class="panel"><h2>Spend by workspace <span class="hint">budget caps</span></h2>
      <table><thead><tr><th>Workspace</th><th>Cost</th><th>Tokens</th><th>Calls</th></tr></thead>
      <tbody>{ws_table}</tbody></table></div>
    <div class="panel"><h2>Providers <span class="hint">health · trailing $/day</span></h2>
      <table><thead><tr><th>Provider</th><th>Cost</th><th>$/day</th><th>Last call</th></tr></thead>
      <tbody>{prov_table}</tbody></table></div>
  </div>
  <div class="grid cols" style="margin-top:16px">
    <div class="panel"><h2>Cost per task type <span class="hint">ROI lens</span></h2>
      <table><thead><tr><th>Task type</th><th>Cost</th><th>Calls</th><th>$/task</th></tr></thead>
      <tbody>{task_table}</tbody></table></div>
    <div class="panel"><h2>Live activity</h2><div class="feed">{feed_html}</div></div>
  </div>
  {runway_panel}
  <div class="panel" style="margin-top:16px"><h2>Billing <span class="hint">prepaid credits · Stripe</span></h2>{billing}</div>
  <div class="foot">Plutus v{__version__} · self-hosted · generated {gen} · live numbers refresh every 5s<br>
    Perseus Computing LLC · <a href="https://perseus.observer/plutus/">perseus.observer/plutus</a></div>
</div>
<script>{POLLER}</script>
</body></html>"""


def simple_page(title: str, heading: str, body: str, *, ok: bool = True) -> str:
    color = "var(--green)" if ok else "var(--coral)"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Plutus — {_e(title)}</title>{FAVICON}
<style>{CSS}</style></head><body><div class="wrap" style="max-width:620px">
<div class="brand" style="margin-bottom:20px"><div class="logo">◆</div><div><h1>Plutus</h1></div></div>
<div class="panel" style="padding:26px 22px">
  <h2 style="color:{color};font-size:18px;padding:0;text-transform:none;letter-spacing:0">{_e(heading)}</h2>
  <div class="muted" style="margin-top:8px">{body}</div>
  <p style="margin-top:20px"><a href="/">← Back to dashboard</a></p>
</div></div></body></html>"""
