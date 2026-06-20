#!/usr/bin/env python3
"""
Plutus — provider credit & spend monitor.

Named for the Greek god of wealth. Plutus watches the money flowing out of
every LLM provider you use so you can balance usage across them efficiently.

Two data sources, fused per provider:
  1. LIVE BALANCE  — for providers that expose a balance API (DeepSeek today).
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
import argparse, json, os, sqlite3, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

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
FOCUS_PROVIDERS = [p.strip() for p in os.environ.get(
    "PLUTUS_PROVIDERS", "deepseek,anthropic,google").split(",") if p.strip()]

# ------------------------------------------------------------- config load ---
def load_yaml(path):
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        sys.stderr.write(f"plutus: could not read config {path}: {e}\n")
        return {}

def load_budgets():
    """Optional: starting credit per provider for no-balance-API providers.
    Format: {"anthropic": {"budget_usd": 250.0, "note": "console grant"}, ...}"""
    if os.path.exists(BUDGETS_FILE):
        try:
            return json.load(open(BUDGETS_FILE))
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
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# provider name in config -> (balance fetcher, ledger billing_provider aliases)
BALANCE_FETCHERS = {
    "deepseek": deepseek_balance,
}
# map config provider name -> the billing_provider strings seen in state.db
LEDGER_ALIASES = {
    "deepseek": ["deepseek"],
    "anthropic": ["anthropic"],
    "google":    ["google", "gemini"],
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
    names = [p for p in FOCUS_PROVIDERS if p in providers]
    # if a focus provider isn't in config, still report it (ledger-only) so
    # the user always sees all three they asked for
    for p in FOCUS_PROVIDERS:
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
        if fetcher and providers.get(name, {}).get("api_key"):
            bal = fetcher(providers[name]["api_key"])
            if bal.get("ok"):
                entry["balance"] = bal["balance_usd"]
                entry["source"] = "live"
                entry["balance_detail"] = bal
            else:
                entry["balance_error"] = bal.get("error")
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
    lines.append(c(" |  _ \\| |_   _| |_ _   _ ___", "33"))
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
    lines.append("  remain = budget - all-time ledger spend (set budgets in plutus.budgets.json)")
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
        cls = "live" if e["source"] == "live" else ("warn" if (days is not None and days < 7) else "")
        days_s = "∞" if days is None else f"{days:.0f}"
        bal = fmt_usd(e["balance"]); rem = fmt_usd(e["remaining"])
        badge = '<span class="b live">LIVE</span>' if e["source"] == "live" else '<span class="b">ledger</span>'
        tr.append(f"""<tr class="{cls}">
<td class="prov">{e['provider']} {badge}</td>
<td class="num big">{bal}</td><td class="num">{rem}</td>
<td class="num">{fmt_usd(s.get('today'))}</td><td class="num">{fmt_usd(s.get('7d'))}</td>
<td class="num">{fmt_usd(s.get('30d'))}</td><td class="num">{fmt_usd(s.get('all'))}</td>
<td class="num">{fmt_usd(e.get('burn_per_day'))}</td><td class="num">{days_s}</td></tr>""")
    note = f'<p class="note">{data["ledger_error"]}</p>' if data.get("ledger_error") else ""
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
.b.live{{background:rgba(45,212,167,.15);color:var(--green)}}
tr.live .big{{color:var(--green)}}
tr.warn td{{background:rgba(239,100,97,.06)}}
tr.warn .num{{color:var(--red)}}
tfoot td{{font-weight:700;background:#11141c;border-bottom:none}}
.note{{color:var(--gold);font-size:13px}}
.legend{{color:var(--dim);font-size:12px;margin-top:16px}}
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
<p class="legend"><b>LIVE</b> = real balance pulled from the provider API · <b>Remaining</b> = budget − all-time ledger spend (set budgets in plutus.budgets.json) · <b>$/day</b> = trailing 7-day burn · <b>Days left</b> = balance ÷ burn.</p>
</div></body></html>"""

def snapshot(data):
    """Append a compact snapshot line for burn-rate history over time."""
    rec = {"t": round(data["generated_at"], 1)}
    for e in data["providers"]:
        rec[e["provider"]] = {
            "bal": e["balance"], "rem": e["remaining"],
            "all": round(e["spend"].get("all", 0), 4),
        }
    with open(SNAPSHOT_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return SNAPSHOT_FILE

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
    with open(BUDGETS_FILE, "w") as f:
        json.dump(budgets, f, indent=2)
    for prov, bal, spent, budget in out:
        print(f"calibrated {prov}: reported balance ${bal:.2f} "
              f"(+ ${spent:.2f} spent = budget ${budget:.2f})")
    return out

# ----------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(description="Plutus — provider credit & spend monitor")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--html", metavar="PATH", help="write HTML dashboard to PATH")
    ap.add_argument("--snapshot", action="store_true", help="append a history snapshot")
    ap.add_argument("--calibrate", action="append", metavar="PROV=BAL", default=[],
                    help="set a provider's true balance, e.g. --calibrate anthropic=74.46 "
                         "(repeatable). Back-solves budget from current ledger spend.")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.calibrate:
        calibrate(args.calibrate)
        # refresh dashboard after recalibration
        data = collect()
        with open(os.path.join(HERE, "plutus.html"), "w") as f:
            f.write(render_html(data))
        print()
        print(render_cli(data, color=not args.no_color))
        return

    data = collect()
    if args.snapshot:
        snapshot(data)
    if args.html:
        with open(args.html, "w") as f:
            f.write(render_html(data))
        sys.stderr.write(f"plutus: wrote {args.html}\n")
    if args.json:
        print(json.dumps(data, indent=2))
    elif not args.html or args.json is False:
        print(render_cli(data, color=not args.no_color))

if __name__ == "__main__":
    main()
