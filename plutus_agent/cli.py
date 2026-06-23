"""``plutus`` command-line interface.

Subcommands:

    plutus init                 create ~/.plutus/{config.yaml,plutus.db}
    plutus serve [--demo]       run the dashboard + API at :8420
    plutus demo                 serve with realistic sample data (no setup)
    plutus status               orgs, balances, Stripe mode
    plutus org create|list      manage organizations
    plutus workspace create|list  manage workspaces
    plutus meter ...            record a usage event (deplete credit)
    plutus topup ...            add prepaid credit
    plutus report ...           monthly PDF/HTML spend report
    plutus alerts [--test]      deliver pending low-balance/budget alerts
    plutus monitor              print live provider runway (monitor bridge)

Everything except Stripe Checkout and email delivery works fully offline.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, __tagline__, config as cfgmod, db, metering, pricing


# ----------------------------------------------------------------- helpers ---
def _conn():
    return db.connect()


def _resolve_org(conn, ident: str | None):
    if ident:
        o = db.get_org(conn, ident) or db.get_org_by_slug(conn, ident)
        if o:
            return o
        for o in db.list_orgs(conn):
            if o["name"] == ident:
                return o
        sys.exit(f"plutus: no organization '{ident}'")
    orgs = db.list_orgs(conn)
    if len(orgs) == 1:
        return orgs[0]
    if not orgs:
        sys.exit("plutus: no organizations. Run `plutus init` or `plutus org create NAME`.")
    sys.exit("plutus: multiple orgs — pass --org <id|slug|name>.")


def _ok(msg):
    print(f"  ✓ {msg}")


# ------------------------------------------------------------------ commands --
def cmd_init(args):
    path, created = cfgmod.ensure_initialized()
    _ok(f"config {'created' if created else 'present'}: {path}")
    conn = _conn()
    db.init_schema(conn)
    _ok(f"database ready: {cfgmod.db_path()}")
    if args.org:
        org = db.create_org(conn, args.org, tier=args.tier, owner_email=args.email)
        _ok(f"organization '{org['name']}' ({org['id']}) on {org['tier']} plan")
        if args.workspace:
            ws = db.create_workspace(conn, org["id"], args.workspace, args.budget)
            _ok(f"workspace '{ws['name']}' ({ws['id']})")
    conn.close()
    print(f"\n  Next: plutus serve   →   http://localhost:8420")
    print(f"        plutus demo    →   explore with sample data\n")


def cmd_serve(args, demo=False):
    from . import server
    cfg = cfgmod.load()
    demo = demo or args.demo
    db_path = str(cfgmod.db_path())
    if demo:
        from . import demo as demo_mod
        db_path = str(cfgmod.home_dir() / "demo.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        for ext in ("", "-wal", "-shm"):
            p = Path(db_path + ext)
            if p.exists():
                p.unlink()
        c = db.connect(db_path)
        org_id = demo_mod.seed(c)
        c.close()
        sys.stderr.write(f"  seeded demo org {org_id}\n")
    server.serve(host=args.host, port=args.port, db_path=db_path, demo=demo,
                 cfg=cfg, open_browser=args.open)


def cmd_status(args):
    cfg = cfgmod.load()
    conn = _conn()
    from .billing import StripeClient
    st = StripeClient(cfg).status()
    print(f"\n  ◆ Plutus v{__version__} — {__tagline__}")
    print(f"    config:   {cfgmod.config_path()}")
    print(f"    database: {cfgmod.db_path()}")
    print(f"    stripe:   {st['mode']}\n")
    orgs = db.list_orgs(conn)
    if not orgs:
        print("    (no organizations — run `plutus init`)\n")
        conn.close()
        return
    print(f"    {'ORG':<22} {'TIER':<11} {'BALANCE':>11} {'MTD SPEND':>11}  WORKSPACES")
    print("    " + "-" * 72)
    for o in orgs:
        s = metering.org_spend_windows(conn, o["id"])
        bal = db.get_balance(conn, o["id"])
        nws = len(db.list_workspaces(conn, o["id"]))
        print(f"    {o['name'][:22]:<22} {o['tier']:<11} "
              f"${bal:>9,.2f} ${s['mtd']['cost']:>9,.2f}  {nws}")
    print()
    conn.close()


def cmd_org(args):
    conn = _conn()
    if args.action == "create":
        org = db.create_org(conn, args.name, tier=args.tier, owner_email=args.email)
        _ok(f"organization '{org['name']}' ({org['id']}) on {org['tier']} plan")
    elif args.action == "list":
        for o in db.list_orgs(conn):
            print(f"  {o['id']}  {o['name']:<24} {o['tier']:<11} "
                  f"${db.get_balance(conn, o['id']):,.2f}")
    conn.close()


def cmd_workspace(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    if args.action == "create":
        ws = db.create_workspace(conn, org["id"], args.name, args.budget)
        cap = f"${args.budget:,.2f}/mo cap" if args.budget else "no cap"
        _ok(f"workspace '{ws['name']}' ({ws['id']}) — {cap}")
    elif args.action == "list":
        for w in db.list_workspaces(conn, org["id"]):
            cap = f"${w['monthly_budget_usd']:,.2f}/mo" if w["monthly_budget_usd"] else "no cap"
            print(f"  {w['id']}  {w['name']:<22} {cap}")
    conn.close()


def cmd_meter(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    cfg = cfgmod.load()
    res = metering.record_usage(
        conn, org["id"], provider=args.provider, model=args.model,
        task_type=args.task, input_tokens=args.input, output_tokens=args.output,
        cache_read_tokens=args.cache, reasoning_tokens=args.reasoning,
        workspace=args.workspace, cost_usd=args.cost, source="cli",
        pricing_overrides=cfg.get("pricing", {}).get("overrides"),
        alert_cfg=cfg.get("alerts", {}),
        block_over_limit=bool(cfg.get("pricing", {}).get("block_over_free_limit")),
    )
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(res), default=str, indent=2))
    elif not res.recorded:
        _ok(f"NOT metered — free-tier token quota reached. Upgrade to Pro for "
            f"unlimited tracking. (set pricing.block_over_free_limit=false to keep recording)")
    else:
        tag = "estimated" if res.estimated else "exact"
        _ok(f"metered {args.provider}/{args.model or '-'} {args.task}: "
            f"${res.cost_usd:.6f} ({tag}) → balance ${res.balance_after:,.2f}")
        if res.over_free_limit:
            print("  ▲ over free-tier token quota — upgrade to Pro for unlimited tracking")
        for a in res.alerts:
            print(f"  ▲ {a['kind']}: {a['message']}")
    conn.close()


def cmd_keys(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    if args.action == "create":
        _, secret = db.create_api_key(conn, org["id"], name=args.name)
        _ok(f"API key for '{org['name']}' — store it now, it won't be shown again:")
        print(f"    {secret}")
    elif args.action == "list":
        keys = db.list_api_keys(conn, org["id"])
        if not keys:
            print("  (no API keys)")
        for k in keys:
            used = "never" if not k["last_used_at"] else f"{int((time.time()-k['last_used_at'])/86400)}d ago"
            print(f"  {k['id']}  {k['prefix']+'…':<22} {(k['name'] or '-'):<18} used {used}")
    elif args.action == "revoke":
        if not args.key_id:
            sys.exit("plutus: pass the key id to revoke, e.g. `plutus keys revoke key_…`")
        if db.revoke_api_key(conn, args.key_id, org["id"]):
            _ok(f"revoked {args.key_id}")
        else:
            print(f"  no active key '{args.key_id}' for this org")
    conn.close()


def cmd_topup(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    row = db.add_ledger(conn, org["id"], args.amount, "topup",
                        reason=args.reason or "manual top-up (cli)")
    _ok(f"added ${args.amount:,.2f} to '{org['name']}' → balance ${row['balance_after']:,.2f}")
    conn.close()


def cmd_report(args):
    from . import reports
    conn = _conn()
    org = _resolve_org(conn, args.org)
    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
    else:
        import datetime as dt
        now = dt.date.today()
        year, month = now.year, now.month
    rep = reports.build_report(conn, org["id"], year, month)
    out = args.out or f"plutus-{org['slug']}-{year}-{month:02d}.pdf"
    path = reports.write(rep, out)
    kind = "PDF" if path.suffix == ".pdf" else "HTML (install reportlab for PDF)"
    _ok(f"{reports.MONTHS[month]} {year} report → {path} [{kind}]")
    _ok(f"total ${rep['total']['cost']:,.2f} · {rep['total']['tokens']:,} tokens · "
        f"{rep['total']['events']:,} calls")
    conn.close()


def cmd_alerts(args):
    from . import alerts
    cfg = cfgmod.load()
    conn = _conn()
    org = _resolve_org(conn, args.org) if args.org else None
    results = alerts.check_and_notify(conn, cfg, org["id"] if org else None)
    for r in results:
        if r.get("dry_run"):
            print(f"  (dry run) {r['org_id']}: {r['pending']} pending — {r.get('detail','')}")
            for m in r.get("would_send", []):
                print(f"      ▲ {m}")
        else:
            print(f"  {r['org_id']}: sent {r['sent']}, {r['pending']} pending"
                  + (f" — error: {r['error']}" if r.get("error") else ""))
    conn.close()


def cmd_monitor(args):
    from . import bridge
    cfg = cfgmod.load()
    data = bridge.runway(cfg.get("monitor", {}))
    if data is None:
        print("  monitor bridge disabled or unavailable. Set monitor.enabled + "
              "monitor.command in config.yaml to fold live provider runway into "
              "the dashboard.")
        return
    print(json.dumps(data, indent=2))


HOOK_MODULE = "plutus_agent.integrations.claude_code_hook"


def _hook_command():
    exe = sys.executable or "python"
    q = f'"{exe}"' if " " in exe else exe
    return f"{q} -m {HOOK_MODULE}"


def _merge_stop_hook(settings: dict, command: str):
    """Merge a Stop-hook entry into a Claude Code settings dict (idempotent).

    Returns (settings, changed). Pure function so it's easy to test.
    """
    hooks = settings.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    for group in stop:
        for h in (group or {}).get("hooks", []):
            if HOOK_MODULE in (h.get("command") or ""):
                return settings, False
    stop.append({"hooks": [{"type": "command", "command": command}]})
    return settings, True


def cmd_install_hook(args):
    command = _hook_command()
    if args.print:
        import json as _json
        snippet = {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": command}]}]}}
        print(_json.dumps(snippet, indent=2))
        print(f"\n  Add the above to {args.path or '~/.claude/settings.json'}")
        return
    path = Path(args.path) if args.path else (Path.home() / ".claude" / "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception as e:
            sys.exit(f"plutus: could not parse {path}: {e}")
        backup = path.with_suffix(path.suffix + ".plutus-bak")
        backup.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    settings, changed = _merge_stop_hook(settings, command)
    if not changed:
        _ok(f"Claude Code hook already installed in {path}")
        return
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    _ok(f"installed Plutus Stop hook → {path}")
    _ok("every Claude Code turn now meters into Plutus (org 'Claude Code', "
        "workspace = project name)")
    print(f"\n  Try it:  run a Claude Code turn, then  plutus serve  → http://localhost:8420")
    print(f"  Set PLUTUS_ORG to attribute turns to a specific org.\n")


def cmd_stripe_setup(args):
    cfg = cfgmod.load()
    key = cfg.get("billing", {}).get("stripe_secret_key") or ""
    if not key:
        sys.exit("plutus: no Stripe key. Set STRIPE_SECRET_KEY (use a sk_test_… key first).")
    try:
        import stripe
    except ImportError:
        sys.exit("plutus: Stripe SDK not installed. Run `pip install 'plutus-agent[stripe]'`.")
    stripe.api_key = key
    mode = "TEST" if key.startswith("sk_test_") else "LIVE"
    print(f"  Stripe {mode} mode — setting up the Pro plan…")

    lookup = "plutus_pro_monthly"
    price = None
    try:
        existing = stripe.Price.list(lookup_keys=[lookup], limit=1, expand=["data.product"])
        if existing.data:
            price = existing.data[0]
            _ok(f"found existing Pro price: {price.id}")
    except Exception:
        pass
    if price is None:
        product = stripe.Product.create(
            name="Plutus Pro",
            description="Plutus Pro — unlimited tracking, prepaid credits, alerts, reports.",
        )
        price = stripe.Price.create(
            product=product.id, unit_amount=2000, currency="usd",
            recurring={"interval": "month"}, lookup_key=lookup,
        )
        _ok(f"created Pro product {product.id} + price {price.id} ($20/mo)")

    # Save onto the file-only config (never the env-merged one) so the live
    # key provided via STRIPE_SECRET_KEY is not persisted to disk.
    to_save = cfgmod.load_base()
    to_save["billing"]["stripe_price_pro"] = price.id
    saved = cfgmod.save(to_save)
    _ok(f"wrote stripe_price_pro to {saved} (key NOT persisted — keep it in env)")
    print("\n  Next:")
    print("    1. plutus serve                       # dashboard with Checkout enabled")
    print("    2. stripe listen --forward-to localhost:8420/webhook/stripe")
    print("    3. Buy credit on the dashboard, or:")
    print("       stripe trigger checkout.session.completed")
    print("    4. Watch the balance top up. See BILLING.md for the full flow.\n")


def cmd_version(args):
    print(f"plutus v{__version__} — {__tagline__}")


def cmd_pricing(args):
    print(f"\n  Plutus plans — {__tagline__}\n")
    for key in ("free", "pro", "enterprise"):
        t = pricing.tier(key)
        price = "custom" if key == "enterprise" else (
            "free" if t.price_usd_month == 0 else f"${t.price_usd_month:.0f}/mo")
        print(f"  {t.name} ({price})")
        for f in t.features:
            print(f"     · {f}")
        print()


# -------------------------------------------------------------------- parser --
def build_parser():
    p = argparse.ArgumentParser(
        prog="plutus", description=f"Plutus — {__tagline__}")
    p.add_argument("--version", action="version", version=f"plutus v{__version__}")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("init", help="create config + database")
    pi.add_argument("--org", help="also create this organization")
    pi.add_argument("--email", help="owner email for the org")
    pi.add_argument("--tier", default="free", choices=["free", "pro", "enterprise"])
    pi.add_argument("--workspace", help="also create this workspace")
    pi.add_argument("--budget", type=float, help="workspace monthly budget USD")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("serve", help="run dashboard + API at :8420")
    ps.add_argument("--host"); ps.add_argument("--port", type=int)
    ps.add_argument("--demo", action="store_true", help="serve realistic sample data")
    ps.add_argument("--open", action="store_true", help="open a browser")
    ps.set_defaults(func=cmd_serve)

    pd = sub.add_parser("demo", help="serve with sample data (zero setup)")
    pd.add_argument("--host"); pd.add_argument("--port", type=int)
    pd.add_argument("--open", action="store_true")
    pd.set_defaults(func=lambda a: cmd_serve(a, demo=True), demo=True)

    sub.add_parser("status", help="show orgs, balances, Stripe mode").set_defaults(func=cmd_status)

    po = sub.add_parser("org", help="manage organizations")
    po.add_argument("action", choices=["create", "list"])
    po.add_argument("name", nargs="?")
    po.add_argument("--tier", default="free", choices=["free", "pro", "enterprise"])
    po.add_argument("--email")
    po.set_defaults(func=cmd_org)

    pw = sub.add_parser("workspace", help="manage workspaces")
    pw.add_argument("action", choices=["create", "list"])
    pw.add_argument("name", nargs="?")
    pw.add_argument("--org"); pw.add_argument("--budget", type=float)
    pw.set_defaults(func=cmd_workspace)

    pm = sub.add_parser("meter", help="record a usage event")
    pm.add_argument("--org"); pm.add_argument("--provider", required=True)
    pm.add_argument("--model"); pm.add_argument("--task", default="general")
    pm.add_argument("--workspace")
    pm.add_argument("--input", type=int, default=0)
    pm.add_argument("--output", type=int, default=0)
    pm.add_argument("--cache", type=int, default=0)
    pm.add_argument("--reasoning", type=int, default=0)
    pm.add_argument("--cost", type=float, help="exact cost USD (else estimated)")
    pm.add_argument("--json", action="store_true")
    pm.set_defaults(func=cmd_meter)

    pk = sub.add_parser("keys", help="manage ingest API keys")
    pk.add_argument("action", choices=["create", "list", "revoke"])
    pk.add_argument("key_id", nargs="?", help="key id (for revoke)")
    pk.add_argument("--name", help="label for a new key")
    pk.add_argument("--org")
    pk.set_defaults(func=cmd_keys)

    pt = sub.add_parser("topup", help="add prepaid credit")
    pt.add_argument("--org"); pt.add_argument("--amount", type=float, required=True)
    pt.add_argument("--reason")
    pt.set_defaults(func=cmd_topup)

    pr = sub.add_parser("report", help="monthly spend report (PDF/HTML)")
    pr.add_argument("--org"); pr.add_argument("--month", help="YYYY-MM (default: current)")
    pr.add_argument("--out", help="output path (.pdf or .html)")
    pr.set_defaults(func=cmd_report)

    pa = sub.add_parser("alerts", help="deliver pending alerts")
    pa.add_argument("--org")
    pa.add_argument("--test", action="store_true", help="(reserved) force-check")
    pa.set_defaults(func=cmd_alerts)

    ph = sub.add_parser("install-claude-hook",
                        help="wire Plutus into Claude Code / Codex (Stop hook)")
    ph.add_argument("--path", help="settings.json path (default ~/.claude/settings.json)")
    ph.add_argument("--print", action="store_true", help="print the snippet, don't write")
    ph.set_defaults(func=cmd_install_hook)

    pss = sub.add_parser("stripe-setup",
                         help="create the $20/mo Pro price in your Stripe account")
    pss.set_defaults(func=cmd_stripe_setup)

    sub.add_parser("monitor", help="print live provider runway (bridge)").set_defaults(func=cmd_monitor)
    sub.add_parser("pricing", help="show plan tiers").set_defaults(func=cmd_pricing)
    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)
    return p


def _force_utf8():
    # Windows consoles default to cp1252 and crash on ◆/✓/em-dash when output is
    # piped. Make stdout/stderr UTF-8 (replace on failure) so Plutus prints the
    # same everywhere.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
