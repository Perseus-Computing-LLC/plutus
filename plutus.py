#!/usr/bin/env python3
"""
Plutus — provider credit & spend monitor.

Named for the Greek god of wealth. Plutus watches the money flowing out of
every LLM provider you use so you can balance usage across them efficiently.

Two data sources, fused per provider:
  1. LIVE BALANCE  — for providers that expose a balance API (DeepSeek, OpenAI).
  2. LOCAL LEDGER  — per-session cost rows Hermes writes to state.db
                     (billing_provider + estimated/actual_cost_usd + tokens).
                     Used for spend, burn-rate, and remaining-budget math on
                     providers that have no balance endpoint (Anthropic, Google).

Outputs: pretty CLI table (default), --json, or --html <path> dashboard.

Config is read from the Hermes config.yaml (provider keys) plus an optional
budgets file (plutus.budgets.json) for providers without a balance API, so
Plutus can show "remaining = budget - ledger_spend".
"""
from __future__ import annotations
import argparse, json, os, sqlite3, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

# ------------------------------------------------------------------ paths ---
HERMES_CONFIG = os.environ.get(
    "PLUTUS_HERMES_CONFIG",
    "/opt/data/webui/minions-hermes-config/config.yaml")
STATE_DB = os.environ.get(
    "PLUTUS_STATE_DB",
    "/opt/data/webui/minions-hermes-config/state.db")
HERE = os.path.dirname(os.path.abspath(__file__))
BUDGETS_FILE = os.environ.get("PLUTUS_BUDGETS", os.path.join(HERE, "plutus.budgets.json"))
SNAPSHOT_FILE = os.environ.get("PLUTUS_SNAPSHOTS", os.path.join(HERE, "plutus.snapshots.jsonl"))

DAY = 86400

# The only providers you care about. Plutus reports exactly these, in this order.
# Override with PLUTUS_PROVIDERS="deepseek,anthropic,google" (comma-separated).
# Also discovers providers from plutus.budgets.json → providers section.
_FOCUS_DEFAULTS = "deepseek,anthropic,google,openai"
FOCUS_PROVIDERS = [p.strip() for p in os.environ.get(
    "PLUTUS_PROVIDERS", _FOCUS_DEFAULTS).split(",") if p.strip()]

def _discover_budgets_providers():
    """Discover additional providers from plutus.budgets.json → providers dict.
    Returns list of provider names from the budgets file's providers section."""
    budgets = load_budgets()
    return list(budgets.get("providers", {}).keys())

def _resolve_providers():
    """Merge env-specified and budgets-discovered providers. Deduplicate."""
    from_budgets = _discover_budgets_providers()
    merged = list(FOCUS_PROVIDERS)
    for p in from_budgets:
        if p not in merged:
            merged.append(p)
    return merged

# ------------------------------------------------------------- config load ---
def load_yaml(path):
    try:
        import yaml
        with open(path, encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        sys.stderr.write(f"plutus: could not read config {path}: {e}\n")
        return {}

def load_budgets():
    """Optional: starting credit per provider for no-balance-API providers.
    Format: {"anthropic": {"budget_usd": 250.0, "note": "console grant"}, ...}"""
    if os.path.exists(BUDGETS_FILE):
        try:
            return json.load(open(BUDGETS_FILE, encoding='utf-8'))
        except Exception as e:
            sys.stderr.write(f"plutus: bad budgets file {BUDGETS_FILE}: {e}\n")
    return {}

# ------------------------------------------------------- live balance APIs ---
def _get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def deepseek_balance(api_key):
    """DeepSeek exposes GET /user/balance -> total_balance in USD."""
    try:
        data = _get("https://api.deepseek.com/user/balance",
                    {"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        infos = data.get("balance_infos") or []
        usd = next((b for b in infos if b.get("currency") == "USD"), infos[0] if infos else {})
        return {
            "balance_usd": float(usd.get("total_balance", 0) or 0),
            "granted_usd": float(usd.get("granted_balance", 0) or 0),
            "topped_up_usd": float(usd.get("topped_up_balance", 0) or 0),
            "available": bool(data.get("is_available")),
            "ok": True,
            "source": "live",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "source": "estimate"}

def openai_balance(api_key):
    """OpenAI balance via billing subscription endpoint.
    Returns hard_limit_usd - total_usage as balance_usd.
    Gracefully falls back on 404/403."""
    try:
        data = _get("https://api.openai.com/v1/dashboard/billing/subscription",
                    {"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        if data.get("object") == "error":
            return {"ok": False, "error": data.get("message"), "source": "estimate"}
        total_granted = data.get("hard_limit_usd", 0)
        total_used = data.get("total_usage", 0)
        return {
            "balance_usd": round(total_granted - total_used, 4),
            "granted_usd": total_granted,
            "topped_up_usd": None,
            "available": True,
            "ok": True,
            "source": "live",
        }
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return {"ok": False, "error": f"OpenAI API returned {e.code} (no billing details or invalid key)", "source": "estimate"}
        return {"ok": False, "error": str(e), "source": "estimate"}
    except Exception as e:
        return {"ok": False, "error": str(e), "source": "estimate"}

def anthropic_balance(api_key):
    """Anthropic — no public balance API yet (github.com/anthropics/anthropic-sdk-python/issues/505).
    Always returns estimate; use budget-based remaining instead."""
    return {"ok": False, "source": "estimate", "note": "Anthropic has no public balance API — using budget estimates"}

def google_balance(api_key):
    """Google Gemini — AI Studio prepay credits, no simple balance REST endpoint.
    Always returns estimate; use budget-based remaining instead."""
    return {"ok": False, "source": "estimate", "note": "Google AI Studio uses prepay credits — using budget estimates"}

# provider name -> balance fetcher (returns {balance_usd, ok, source: 'live'|'estimate'})
BALANCE_FETCHERS = {
    "deepseek":  deepseek_balance,
    "openai":    openai_balance,
    "anthropic": anthropic_balance,
    "google":    google_balance,
}

def _generic_balance(api_key, endpoint):
    """Generic balance fetcher for custom providers with OpenAI-compatible endpoints.
    Tries GET {endpoint}/user/balance or {endpoint}/billing/credits if available."""
    try:
        # Try common balance endpoint patterns
        for path in ["/user/balance", "/billing/credits", "/v1/credits"]:
            try:
                data = _get(f"{endpoint.rstrip('/')}{path}",
                    {"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
                if isinstance(data, dict):
                    balance = data.get("total_balance") or data.get("balance_usd") or data.get("credits")
                    if balance is not None:
                        return {"balance_usd": float(balance), "ok": True, "source": "live"}
            except Exception:
                continue
        return {"ok": False, "source": "estimate", "note": "Custom provider — no live balance endpoint found"}
    except Exception as e:
        return {"ok": False, "error": str(e), "source": "estimate"}
# map config provider name -> the billing_provider strings seen in state.db
LEDGER_ALIASES = {
    "deepseek":  ["deepseek"],
    "anthropic": ["anthropic"],
    "google":    ["google", "gemini"],
    "openai":    ["openai"],
}

# ----------------------------------------------------------- local ledger ---
def ledger_spend(db_path):
    """Aggregate per-session cost rows by billing_provider over several windows.
    Prefers actual_cost_usd, falls back to estimated_cost_usd."""
    out = {}
    if not os.path.exists(db_path):
        return out, "(state.db not found)"
    now = time.time()
    windows = {"today": now - DAY, "7d": now - 7 * DAY, "30d": now - 30 * DAY, "all": 0}
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = c.execute("""
            select coalesce(nullif(billing_provider,''),'unknown') as prov,
                   started_at,
                   coalesce(nullif(actual_cost_usd,0), estimated_cost_usd, 0) as cost,
                   coalesce(input_tokens,0), coalesce(output_tokens,0),
                   coalesce(cache_read_tokens,0), coalesce(reasoning_tokens,0)
            from sessions
        """)
        rows = cur.fetchall()
        c.close()
    except Exception as e:
        return out, f"(ledger error: {e})"
    for prov, started, cost, itok, otok, ctok, rtok in rows:
        d = out.setdefault(prov, {w: 0.0 for w in windows})
        d.setdefault("in_tok", 0); d.setdefault("out_tok", 0)
        d.setdefault("sessions", 0); d.setdefault("last_ts", 0)
        st = started or 0
        for w, floor in windows.items():
            if st >= floor:
                d[w] += float(cost or 0)
        d["in_tok"] += int(itok); d["out_tok"] += int(otok)
        d["sessions"] += 1
        if st > d["last_ts"]:
            d["last_ts"] = st
    return out, None

# ------------------------------------------------------------- assemble ----
def collect():
    cfg = load_yaml(HERMES_CONFIG)
    providers = cfg.get("providers", {}) or {}
    budgets = load_budgets()
    ledger, ledger_err = ledger_spend(STATE_DB)

    # union of configured providers and providers seen in the ledger
    names = [p for p in _resolve_providers() if p in providers]
    # if a focus provider isn't in config, still report it (ledger-only) so
    # the user always sees all they asked for
    for p in _resolve_providers():
        if p not in names:
            names.append(p)
    # fold ledger aliases back to canonical config names where possible
    alias_to_canon = {}
    for canon, aliases in LEDGER_ALIASES.items():
        for a in aliases:
            alias_to_canon[a] = canon
    report = []
    handled_ledger_keys = set()
    for name in names:
        aliases = LEDGER_ALIASES.get(name, [name])
        spend = {"today": 0.0, "7d": 0.0, "30d": 0.0, "all": 0.0,
                 "in_tok": 0, "out_tok": 0, "sessions": 0, "last_ts": 0}
        for a in aliases:
            if a in ledger:
                handled_ledger_keys.add(a)
                for k in ("today", "7d", "30d", "all", "in_tok", "out_tok", "sessions"):
                    spend[k] += ledger[a].get(k, 0)
                spend["last_ts"] = max(spend["last_ts"], ledger[a].get("last_ts", 0))

        entry = {
            "provider": name,
            "spend": spend,
            "balance": None,
            "budget": None,
            "remaining": None,
            "source": "ledger",
        }
        # live balance?
        fetcher = BALANCE_FETCHERS.get(name)
        if not fetcher:
            # Check if custom provider is defined in budgets.json
            provider_cfg = budgets.get("providers", {}).get(name)
            if provider_cfg and provider_cfg.get("endpoint"):
                api_key = providers.get(name, {}).get("api_key")
                endpoint = provider_cfg["endpoint"]
                fetcher = lambda ak=api_key, ep=endpoint: _generic_balance(ak, ep)
        if fetcher and providers.get(name, {}).get("api_key"):
            bal = fetcher(providers[name]["api_key"])
            if bal.get("ok"):
                entry["balance"] = bal["balance_usd"]
                entry["source"] = bal.get("source", "live")
            else:
                entry["balance_error"] = bal.get("error")
                entry["source"] = bal.get("source", "estimate")
        # budget-based remaining for no-balance providers
        b = budgets.get(name)
        if b and isinstance(b, dict) and float(b.get("budget_usd") or 0) > 0:
            entry["budget"] = float(b["budget_usd"])
            entry["budget_note"] = b.get("note", "")
            entry["remaining"] = round(entry["budget"] - spend["all"], 4)
        # burn rate (last 7d / 7)
        entry["burn_per_day"] = round(spend["7d"] / 7.0, 4)
        # days left projection
        live_or_rem = entry["balance"] if entry["balance"] is not None else entry["remaining"]
        if live_or_rem is not None and entry["burn_per_day"] > 0:
            entry["days_left"] = round(live_or_rem / entry["burn_per_day"], 1)
        else:
            entry["days_left"] = None
        report.append(entry)

    return {
        "generated_at": time.time(),
        "providers": report,
        "ledger_error": ledger_err,
        "state_db": STATE_DB,
        "config": HERMES_CONFIG,
    }

# --------------------------------------------------------------- renderers --
def fmt_usd(v):
    return "—" if v is None else f"${v:,.2f}"

def render_cli(data, color=True):
    def c(s, code):
        return f"\033[{code}m{s}\033[0m" if color and sys.stdout.isatty() else s
    lines = []
    gen = datetime.fromtimestamp(data["generated_at"]).strftime("%Y-%m-%d %H:%M:%S")
    lines.append(c("  ____  _       _", "33"))
    lines.append(c(" |  _ \\| |_   _| |_ _   _ __", "33"))
    lines.append(c(" | |_) | | | | | __| | | / __|   god of wealth", "33"))
    lines.append(c(" |  __/| | |_| | |_| |_| \\__ \\   provider credit monitor", "33"))
    lines.append(c(" |_|   |_|\\__,_|\\__|\\__,_|___/", "33"))
    lines.append(f"  generated {gen}")
    lines.append("")
    hdr = f"{'PROVIDER':<12} {'BALANCE':>10} {'REMAIN':>10} {'TODAY':>9} {'7D':>9} {'30D':>9} {'ALL':>10} {'$/DAY':>8} {'DAYS':>6} SRC"
    lines.append(c(hdr, "1"))
    lines.append("-" * len(hdr))
    tot = {"today": 0, "7d": 0, "30d": 0, "all": 0}
    rows = sorted(data["providers"], key=lambda e: e["spend"].get("all", 0), reverse=True)
    for e in rows:
        s = e["spend"]
        for k in tot:
            tot[k] += s.get(k, 0)
        days = e.get("days_left")
        days_s = "∞" if days is None else f"{days:.0f}"
        bal = fmt_usd(e["balance"])
        rem = fmt_usd(e["remaining"])
        line = (f"{e['provider']:<12} {bal:>10} {rem:>10} "
                f"{fmt_usd(s.get('today')):>9} {fmt_usd(s.get('7d')):>9} "
                f"{fmt_usd(s.get('30d')):>9} {fmt_usd(s.get('all')):>10} "
                f"{fmt_usd(e.get('burn_per_day')):>8} {days_s:>6} {e['source']}")
        if e["source"] == "live":
            line = c(line, "32")
        elif e["source"] == "estimate":
            line = c(line, "33")  # yellow
        elif days is not None and days < 7:
            line = c(line, "31")
        lines.append(line)
    lines.append("-" * len(hdr))
    lines.append(f"{'TOTAL':<12} {'':>10} {'':>10} "
                 f"{fmt_usd(tot['today']):>9} {fmt_usd(tot['7d']):>9} "
                 f"{fmt_usd(tot['30d']):>9} {fmt_usd(tot['all']):>10}")
    if data.get("ledger_error"):
        lines.append("")
        lines.append(c(f"  ledger note: {data['ledger_error']}", "33"))
    lines.append("")
    lines.append("  live  = real balance from provider API")
    lines.append("  est   = estimate (budget - spend, no live API — set budgets in plutus.budgets.json)")
    return "\n".join(lines)

def render_html(data):
    gen = datetime.fromtimestamp(data["generated_at"]).strftime("%Y-%m-%d %H:%M:%S")
    rows = sorted(data["providers"], key=lambda e: e["spend"].get("all", 0), reverse=True)
    tr = []
    tot = {"today": 0, "7d": 0, "30d": 0, "all": 0}
    for e in rows:
        s = e["spend"]
        for k in tot:
            tot[k] += s.get(k, 0)
        days = e.get("days_left")
        src = e["source"]
        cls = "live" if src == "live" else ("warn" if (days is not None and days < 7) else "")
        days_s = "∞" if days is None else f"{days:.0f}"
        bal = fmt_usd(e["balance"]); rem = fmt_usd(e["remaining"])
        badge_cls = {"live": "b live", "estimate": "b est", "ledger": "b"}.get(src, "b")
        badge_label = {"live": "LIVE", "estimate": "EST", "ledger": "ledger"}.get(src, src)
        badge = f'<span class="{badge_cls}">{badge_label}</span>'
        tr.append(f"""<tr class="{cls}">
<td class="prov">{e['provider']} {badge}</td>
<td class="num big">{bal}</td><td class="num">{rem}</td>
<td class="num">{fmt_usd(s.get('today'))}</td><td class="num">{fmt_usd(s.get('7d'))}</td>
<td class="num">{fmt_usd(s.get('30d'))}</td><td class="num">{fmt_usd(s.get('all'))}</td>
<td class="num">{fmt_usd(e.get('burn_per_day'))}</td><td class="num">{days_s}</td></tr>""")
    note = f'<p class="note">{data["ledger_error"]}</p>' if data.get("ledger_error") else ""
    
    # Build trends section from snapshot history
    snapshot_history = _load_snapshot_history()
    trends_html = render_trends_html(data, snapshot_history)
    
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plutus — provider credit monitor</title>
<style>
:root{{--bg:#0d0f14;--card:#161a23;--line:#252b38;--txt:#e8eaf0;--dim:#8b93a7;--gold:#e9c46a;--green:#2dd4a7;--red:#ef6461}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:1040px;margin:0 auto;padding:32px 20px}}
h1{{font-size:26px;margin:0;color:var(--gold);letter-spacing:.5px}}
h1 span{{color:var(--dim);font-size:14px;font-weight:400;margin-left:10px}}
.sub{{color:var(--dim);font-size:13px;margin:4px 0 24px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
th,td{{padding:12px 14px;text-align:right;border-bottom:1px solid var(--line)}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);font-weight:600;background:#11141c}}
th:first-child,td:first-child{{text-align:left}}
.num{{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.big{{font-size:16px;color:var(--gold)}}
.prov{{font-weight:600}}
.b{{font-size:10px;padding:2px 7px;border-radius:20px;background:#222838;color:var(--dim);margin-left:8px;vertical-align:middle}}
.b.live{{background:rgba(45,212,167,.15);color:var(--green)}}\n.b.est{{background:rgba(233,196,106,.15);color:var(--gold)}}\ntr.live .big{{color:var(--green)}}
tr.warn td{{background:rgba(239,100,97,.06)}}
tr.warn .num{{color:var(--red)}}
tfoot td{{font-weight:700;background:#11141c;border-bottom:none}}
.note{{color:var(--gold);font-size:13px}}
.legend{{color:var(--dim);font-size:12px;margin-top:16px}}
{_TRENDS_CSS}
</style></head><body><div class="wrap">
<h1>Plutus <span>god of wealth · provider credit monitor</span></h1>
<p class="sub">generated {gen}</p>
<table><thead><tr>
<th>Provider</th><th>Balance</th><th>Remaining</th><th>Today</th><th>7d</th><th>30d</th><th>All-time</th><th>$/day</th><th>Days left</th>
</tr></thead><tbody>
{''.join(tr)}
</tbody><tfoot><tr><td>Total</td><td></td><td></td>
<td class="num">{fmt_usd(tot['today'])}</td><td class="num">{fmt_usd(tot['7d'])}</td>
<td class="num">{fmt_usd(tot['30d'])}</td><td class="num">{fmt_usd(tot['all'])}</td><td></td><td></td></tr></tfoot></table>
{note}
{trends_html}
<p class="legend"><b>LIVE</b> = real balance pulled from the provider API · <b>EST</b> = estimate (budget − spend, no live API) · <b>$/day</b> = trailing 7-day burn · <b>Days left</b> = balance ÷ burn.</p>
</div></body></html>"""

def snapshot(data):
    """Append a compact snapshot line for burn-rate history over time."""
    rec = {"t": round(data["generated_at"], 1)}
    for e in data["providers"]:
        rec[e["provider"]] = {
            "bal": e["balance"], "rem": e["remaining"],
            "all": round(e["spend"].get("all", 0), 4),
            "src": e.get("source", "estimate"),
        }
    with open(SNAPSHOT_FILE, "a", encoding='utf-8') as f:
        f.write(json.dumps(rec) + "\n")
    return SNAPSHOT_FILE

# --------------------------------------------------------------- trends ---
def _load_snapshot_history():
    """Load snapshot lines into a list of {provider: {bal, rem, all}, t} dicts."""
    if not os.path.exists(SNAPSHOT_FILE):
        return []
    out = []
    with open(SNAPSHOT_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out

def _linear_regression(points):
    """Simple linear regression: returns (slope, intercept) or None."""
    n = len(points)
    if n < 3:
        return None
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 0.0001:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return (slope, intercept)

def _render_sparkline(points, width=120, height=30, color="#e9c46a"):
    """Render an inline SVG sparkline from data points [(x, y), ...]."""
    if len(points) < 2:
        return ""
    ys = [p[1] for p in points]
    y_min = min(ys); y_max = max(ys)
    if y_max - y_min < 0.01:
        y_min -= 1; y_max += 1
    x_min = points[0][0]; x_max = points[-1][0]
    if x_max - x_min < 1:
        x_max = x_min + 1
    
    def px(x, y):
        return f"{(x - x_min)/(x_max - x_min) * width:.1f},{(1 - (y - y_min)/(y_max - y_min)) * height:.1f}"
    
    path = " ".join(f"{'L' if i>0 else 'M'}{px(p[0], p[1])}" for i, p in enumerate(points))
    y_label = f"${y_max:.1f}"
    return f'<svg width="{width}" height="{height}" style="vertical-align:middle;margin:0 4px">' \
           f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{path}"/>' \
           f'<text x="0" y="8" fill="#8b93a7" font-size="8">{y_label}</text></svg>'

def render_trends_html(data, snapshot_history):
    """Build burn-rate trend section as an HTML block with SVG sparklines."""
    if not snapshot_history:
        return ""
    
    now = time.time()
    providers = {e["provider"]: e for e in data["providers"]}
    
    rows = []
    for pname, pinfo in providers.items():
        # Extract all-time spend over time for this provider
        points = []
        for snap in snapshot_history:
            t = snap.get("t", 0)
            provider_data = snap.get(pname)
            if provider_data:
                all_spend = provider_data.get("all", 0)
                if all_spend > 0 or len(points) > 0:
                    points.append((t, all_spend))
        
        if len(points) < 3:
            continue
        
        # Linear regression on spend over time
        points_relative = [(p[0] - points[0][0], p[1]) for p in points]
        trend = _linear_regression(points_relative)
        if trend is None:
            continue
        
        slope, intercept = trend
        # Make relative points for sparkline (seconds -> hours)
        spark_points = [(p[0] / 3600.0, p[1]) for p in points_relative]
        
        # Forecast: project forward 30 days
        spend_now = points_relative[-1][1]
        spend_7d = intercept + slope * 7 * DAY
        spend_30d = intercept + slope * 30 * DAY
        
        daily_burn = max(slope, 0) * DAY
        fore_7 = f"${spend_7d:.2f}" if daily_burn > 0 else "—"
        fore_30 = f"${spend_30d:.2f}" if daily_burn > 0 else "—"
        
        spark = _render_sparkline(spark_points[-48:], width=100, height=24,
                                  color="#2dd4a7" if pinfo["source"] == "live" else "#e9c46a")
        
        rows.append(f"""<tr>
<td class="prov">{pname}</td>
<td class="num">{fmt_usd(daily_burn) if daily_burn > 0 else '—'}</td>
<td class="num">{fore_7}</td>
<td class="num">{fore_30}</td>
<td>{spark}</td></tr>""")
    
    if not rows:
        return ""
    
    return f"""
<div class="trends-section">
<h2>Burn-Rate Trends <span>from snapshot history</span></h2>
<table><thead><tr>
<th>Provider</th><th class="num">Daily burn</th><th class="num">7d forecast</th><th class="num">30d forecast</th><th>Trend</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<p class="legend">Forecasts use linear regression from snapshot history. Sparklines show last 48 data points.</p>
</div>
"""

# Additional CSS for trends section
_TRENDS_CSS = """
.trends-section{{margin-top:32px}}
.trends-section h2{{font-size:18px;color:var(--gold);margin:0 0 12px}}
.trends-section h2 span{{color:var(--dim);font-size:12px;font-weight:400;margin-left:8px}}
"""

# ------------------------------------------------------------- calibrate ---
def calibrate(pairs):
    """Set each provider's budget so that 'remaining' == the real balance you
    report right now. budget = reported_balance + current all-time ledger spend.
    Going forward, remaining = budget - ledger_spend decrements correctly.
    pairs: list of "provider=balance" strings."""
    ledger, _ = ledger_spend(STATE_DB)
    budgets = load_budgets()
    out = []
    for pair in pairs:
        if "=" not in pair:
            sys.stderr.write(f"plutus: bad --calibrate '{pair}', want provider=balance\n")
            continue
        prov, val = pair.split("=", 1)
        prov = prov.strip();
        try:
            bal = float(val)
        except ValueError:
            sys.stderr.write(f"plutus: bad balance '{val}' for {prov}\n")
            continue
        spent = 0.0
        for a in LEDGER_ALIASES.get(prov, [prov]):
            if a in ledger:
                spent += ledger[a].get("all", 0)
        budget = round(bal + spent, 4)
        budgets[prov] = {"budget_usd": budget,
                         "note": f"calibrated {datetime.now().strftime('%Y-%m-%d')}: "
                                 f"balance ${bal:.2f} + spent ${spent:.2f}"}
        out.append((prov, bal, spent, budget))
    budgets.setdefault("_comment",
        "budget_usd = starting/known credit. remaining = budget - all-time ledger spend. "
        "Recalibrate with: python3 plutus.py --calibrate anthropic=NN.NN")
    with open(BUDGETS_FILE, "w", encoding='utf-8') as f:
        json.dump(budgets, f, indent=2)
    for prov, bal, spent, budget in out:
        print(f"calibrated {prov}: reported balance ${bal:.2f} "
              f"(+ ${spent:.2f} spent = budget ${budget:.2f})")
    # Issue #9: Show copy-paste command with current budget values
    if out:
        print("\nRe-run with: python3 plutus.py --calibrate " +
              " ".join(f"{p[0]}={budgets.get(p[0], {}).get('budget_usd', 'NN.NN')}"
                       for p in out))
    return out

# --------------------------------------------------------------- forecast ---
def do_forecast(provider_name, as_json):
    """Forecast budget exhaustion using 7-day and 30-day average spend rates."""
    data = collect()
    forecasts = []
    for e in data["providers"]:
        if provider_name and e["provider"] != provider_name:
            continue
        burn_7d = e["spend"].get("7d", 0) / 7.0
        burn_30d = e["spend"].get("30d", 0) / 30.0
        balance = e["balance"] if e["balance"] is not None else e["remaining"]

        if balance is not None and balance > 0 and burn_7d > 0:
            days_7d = balance / burn_7d
            exhaust_date_7d = datetime.now() + timedelta(days=days_7d)
            forecasts.append({
                "provider": e["provider"],
                "burn_rate_avg": "7d",
                "burn_per_day": round(burn_7d, 4),
                "remaining_usd": round(balance, 4),
                "exhaustion_date": exhaust_date_7d.strftime("%Y-%m-%d"),
            })
            # Only show 30d if burn rate differs meaningfully
            if burn_30d > 0 and abs(burn_7d - burn_30d) > 0.001:
                days_30d = balance / burn_30d
                exhaust_date_30d = datetime.now() + timedelta(days=days_30d)
                forecasts.append({
                    "provider": e["provider"],
                    "burn_rate_avg": "30d",
                    "burn_per_day": round(burn_30d, 4),
                    "remaining_usd": round(balance, 4),
                    "exhaustion_date": exhaust_date_30d.strftime("%Y-%m-%d"),
                })
        elif balance is not None and balance <= 0:
            forecasts.append({
                "provider": e["provider"],
                "message": "Balance exhausted or non-positive."
            })
        else:
            forecasts.append({
                "provider": e["provider"],
                "message": "No balance or burn rate data available."
            })

    if as_json:
        print(json.dumps(forecasts, indent=2))
    else:
        lines = []
        lines.append("\nPlutus Forecast:")
        lines.append("-" * 65)
        hdr = f"{'PROVIDER':<12} {'AVG':<5} {'$/DAY':>9} {'REMAIN':>10} {'EXHAUSTS':>12}"
        lines.append(hdr)
        lines.append("-" * len(hdr))
        for f in forecasts:
            if "message" in f:
                lines.append(f"{f['provider']:<12} {f['message']}")
            else:
                lines.append(f"{f['provider']:<12} {f['burn_rate_avg']:<5} "
                             f"{fmt_usd(f['burn_per_day']):>9} "
                             f"{fmt_usd(f['remaining_usd']):>10} "
                             f"{f['exhaustion_date']:>12}")
        lines.append("")
        print("\n".join(lines))

# ----------------------------------------------------------------- tokens ---
def do_tokens(text):
    """Count tokens using tiktoken if available, else word-based estimate."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        tokens = len(enc.encode(text))
        print(f"Tokens (tiktoken): {tokens}")
    except ImportError:
        tokens = len(text.split())
        print(f"tiktoken not available, falling back to word count.")
        print(f"Tokens (word count estimate): {tokens}")

# ------------------------------------------------------------------ alert ---
def do_alert(dry_run, channel=None):
    """Send low-balance alerts via configured channels (email, Discord, ntfy).
    Supports --dry-run to print instead of send.
    Supports --channel <name> to restrict to a specific channel."""
    budgets = load_budgets()
    data = collect()
    alerts_cfg = budgets.get("alerts", {})

    # Gather alert conditions (same for all channels)
    email_cfg = alerts_cfg.get("email", {})
    balance_threshold_usd = email_cfg.get("balance_threshold_usd")
    days_left_threshold = email_cfg.get("days_left_threshold")

    alert_messages = []
    for e in data["providers"]:
        provider_name = e["provider"]
        balance = e["balance"] if e["balance"] is not None else e["remaining"]
        days_left = e["days_left"]

        if balance_threshold_usd is not None and balance is not None and balance < balance_threshold_usd:
            alert_messages.append(
                f"  - {provider_name}: Balance ${balance:.2f} is below threshold ${balance_threshold_usd:.2f}")
        if days_left_threshold is not None and days_left is not None and days_left < days_left_threshold:
            alert_messages.append(
                f"  - {provider_name}: {days_left:.1f} days left is below threshold {days_left_threshold:.1f} days")

    if not alert_messages:
        print("No low balance conditions detected. No alert sent.")
        return

    subject = "Plutus Low Balance Alert"
    body = ("Plutus detected the following low balance conditions:\n\n" +
            "\n".join(alert_messages) +
            "\n\nCheck `plutus` for full details.\n")

    sent = 0

    # ---- Email (Himalaya) ----
    if not channel or channel == "email":
        to_email = email_cfg.get("to")
        if to_email:
            if dry_run:
                print("--- DRY RUN: Email ---")
                print(f"To: {to_email}\nSubject: {subject}\n{body}")
                print("-----------------------")
            else:
                try:
                    cmd = ["himalaya", "send", "-t", to_email, "-s", subject, "-"]
                    proc = subprocess.run(cmd, input=body.encode("utf-8"),
                                          capture_output=True, timeout=30)
                    if proc.returncode == 0:
                        print(f"Email alert sent to {to_email}.")
                        sent += 1
                    else:
                        print(f"Himalaya exited with code {proc.returncode}: {proc.stderr.decode().strip()}")
                except FileNotFoundError:
                    print("Error: 'himalaya' not found. Install Himalaya CLI for email alerts.")
                except subprocess.TimeoutExpired:
                    print("Error: Himalaya timed out.")
                except Exception as e:
                    print(f"Error sending email: {e}")

    # ---- Discord webhook ----
    if not channel or channel == "discord":
        discord_cfg = alerts_cfg.get("discord", {})
        webhook_url = discord_cfg.get("webhook_url")
        if webhook_url:
            discord_body = f"**{subject}**\n\n{body}"
            if dry_run:
                print("--- DRY RUN: Discord ---")
                print(f"Webhook URL: {webhook_url[:60]}...\n{discord_body}")
                print("-------------------------")
            else:
                try:
                    req_data = json.dumps({"content": discord_body}).encode("utf-8")
                    req = urllib.request.Request(webhook_url, data=req_data,
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10)
                    print("Discord alert sent.")
                    sent += 1
                except Exception as e:
                    print(f"Error sending Discord alert: {e}")

    # ---- ntfy ----
    if not channel or channel == "ntfy":
        ntfy_cfg = alerts_cfg.get("ntfy", {})
        ntfy_topic = ntfy_cfg.get("topic")
        ntfy_server = ntfy_cfg.get("server", "https://ntfy.sh")
        if ntfy_topic:
            ntfy_url = f"{ntfy_server}/{ntfy_topic}"
            if dry_run:
                print("--- DRY RUN: ntfy ---")
                print(f"URL: {ntfy_url}\nSubject: {subject}\n{body}")
                print("----------------------")
            else:
                try:
                    req_data = body.encode("utf-8")
                    req = urllib.request.Request(ntfy_url, data=req_data,
                        headers={"Title": subject, "Priority": "high"})
                    urllib.request.urlopen(req, timeout=10)
                    print(f"ntfy alert sent to {ntfy_topic}.")
                    sent += 1
                except Exception as e:
                    print(f"Error sending ntfy alert: {e}")

    if sent == 0 and not dry_run:
        print("No alert channels configured. Add 'alerts' to plutus.budgets.json.")

# ----------------------------------------------------------------- main ----
VERSION = "0.1.1"

def main():
    ap = argparse.ArgumentParser(description="Plutus — provider credit & spend monitor")
    ap.add_argument("--version", action="version", version=f"plutus v{VERSION}")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--html", metavar="PATH", help="write HTML dashboard to PATH")
    ap.add_argument("--snapshot", action="store_true", help="append a history snapshot")
    ap.add_argument("--calibrate", action="append", metavar="PROV=BAL", default=[],
                    help="set a provider's true balance, e.g. --calibrate anthropic=74.46 "
                         "(repeatable). Back-solves budget from current ledger spend.")
    ap.add_argument("--no-color", action="store_true")

    subparsers = ap.add_subparsers(dest="command")

    # forecast subcommand
    forecast_ap = subparsers.add_parser("forecast", help="forecast budget exhaustion")
    forecast_ap.add_argument("--json", action="store_true", help="emit raw JSON")
    forecast_ap.add_argument("provider", nargs="?", help="forecast for a specific provider")

    # tokens subcommand
    tokens_ap = subparsers.add_parser("tokens", help="count tokens in text")
    tokens_ap.add_argument("text", help="text to count tokens for")

    # alert subcommand
    alert_ap = subparsers.add_parser("alert", help="send low-balance alerts via configured channels")
    alert_ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    alert_ap.add_argument("--channel", choices=["email", "discord", "ntfy"],
                          help="restrict to a specific channel (default: all configured)")
    alert_ap.add_argument("--email", "--thresh", metavar="DOLLARS", type=float,
                          help="override email balance threshold")
    alert_ap.add_argument("--days", metavar="N", type=float,
                          help="override email days-left threshold")

    args = ap.parse_args()

    # Dispatch subcommands
    if args.command == "forecast":
        do_forecast(args.provider, args.json)
        return
    elif args.command == "tokens":
        do_tokens(args.text)
        return
    elif args.command == "alert":
        do_alert(args.dry_run, args.channel)
        return

    # Legacy --calibrate flag
    if args.calibrate:
        calibrate(args.calibrate)
        # refresh dashboard after recalibration
        data = collect()
        with open(os.path.join(HERE, "plutus.html"), "w", encoding='utf-8') as f:
            f.write(render_html(data))
        print()
        print(render_cli(data, color=not args.no_color))
        return

    # Default: show main table
    data = collect()
    if args.snapshot:
        snapshot(data)
    if args.html:
        with open(args.html, "w", encoding='utf-8') as f:
            f.write(render_html(data))
        sys.stderr.write(f"plutus: wrote {args.html}\n")
    if args.json:
        print(json.dumps(data, indent=2))
    elif not args.html or args.json is False:
        print(render_cli(data, color=not args.no_color))

if __name__ == "__main__":
    main()
