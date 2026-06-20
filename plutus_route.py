#!/usr/bin/env python3
"""
plutus_route.py — Plutus's balancing arm.

Reads live runway from plutus.py, then rebalances Hermes model routing so the
provider with the MOST runway runs its flagship as primary, and the other two
providers supply the best subtask/fallback models. Edits config.yaml in place
(targeted, backed up, re-verified — never a blind full rewrite).

Routing policy
--------------
1. Rank deepseek / anthropic / google by projected days-left (runway).
   - deepseek: live API balance / burn
   - anthropic, google: (calibrated budget - ledger spend) / burn
   - infinite/unknown runway sorts last-resort high (lots of headroom).
2. PRIMARY  = flagship model of the highest-runway provider.
3. FALLBACKS = the other two providers, flagship first (capable subtask work),
   then their fast/cheap model for lighter subtasks.
4. DELEGATION (subagent/subtask model) = best fast model of the highest-runway
   NON-primary provider, so heavy primary spend doesn't bleed onto subtasks.

Model IDs below are verified live against each provider's /models endpoint.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("PLUTUS_HERMES_CONFIG",
                        "/opt/data/webui/minions-hermes-config/config.yaml")
PLUTUS = os.path.join(HERE, "plutus.py")
ROUTE_LOG = os.path.join(HERE, "plutus.routing.jsonl")

VERSION = "0.1.0"

# Verified model catalogs (from live /models calls 2026-06-19).
# Override any provider's model via plutus.budgets.json → models.flagship / models.subtask.
FLAGSHIP = {
    "deepseek":  "deepseek-v4-pro",
    "anthropic": "claude-opus-4-8",
    "google":    "gemini-3.1-pro-preview",
}
SUBTASK = {  # fast / cheaper model for delegation + light fallbacks
    "deepseek":  "deepseek-v4-flash",
    "anthropic": "claude-sonnet-4-5-20250929",
    "google":    "gemini-2.5-flash",
}

def _load_models():
    """Merge model overrides from plutus.budgets.json into FLAGSHIP/SUBTASK defaults."""
    budgets_path = os.environ.get("PLUTUS_BUDGETS",
                                   os.path.join(HERE, "plutus.budgets.json"))
    try:
        if os.path.exists(budgets_path):
            cfg = json.load(open(budgets_path, encoding='utf-8'))
            models = cfg.get("models", {})
            for kind, target in (("flagship", FLAGSHIP), ("subtask", SUBTASK)):
                if kind in models and isinstance(models[kind], dict):
                    for prov, model_id in models[kind].items():
                        target[prov] = model_id
    except Exception:
        pass  # budgets.json is optional; defaults are always present

_load_models()
PROVIDERS = ["deepseek", "anthropic", "google"]

# ------------------------------------------------------------- routing policies ---
# Estimated cost per 1M input tokens (USD). Updated 2026-06-20.
# For models not in this table, cost-cap policy treats them as unknown (skipped).
MODEL_COST_PER_1M_IN = {
    "deepseek-v4-pro":  2.50,
    "deepseek-v4-flash": 0.26,
    "claude-opus-4-8":  15.00,
    "claude-sonnet-4-5-20250929": 3.00,
    "gemini-3.1-pro-preview": 2.50,
    "gemini-2.5-flash":  0.15,
}

# Estimated latency tier (1=fastest, 5=slowest). For latency-weighted routing.
MODEL_LATENCY_TIER = {
    "deepseek-v4-pro":  3,
    "deepseek-v4-flash": 1,
    "claude-opus-4-8":  4,
    "claude-sonnet-4-5-20250929": 2,
    "gemini-3.1-pro-preview": 3,
    "gemini-2.5-flash":  1,
}

# Quality benchmark score (0-100). For quality-floor filtering.
# Rough scores based on LMSYS/Arena Elo approximations June 2026.
MODEL_QUALITY_SCORE = {
    "deepseek-v4-pro":  85,
    "deepseek-v4-flash": 65,
    "claude-opus-4-8":  88,
    "claude-sonnet-4-5-20250929": 78,
    "gemini-3.1-pro-preview": 84,
    "gemini-2.5-flash":  72,
}

def _load_policy_config():
    """Read routing.policy from plutus.budgets.json."""
    budgets_path = os.environ.get("PLUTUS_BUDGETS",
                                   os.path.join(HERE, "plutus.budgets.json"))
    try:
        if os.path.exists(budgets_path):
            cfg = json.load(open(budgets_path, encoding='utf-8'))
            return cfg.get("routing", {}).get("policy", "runway")
    except Exception:
        pass
    return "runway"

def _apply_policy(order, rw, policy_name, policy_config):
    """Apply a routing policy to reorder/suppress providers.
    
    Policies are stackable when comma-separated: 'cost-cap,quality-floor'.
    Returns (reordered_providers, skipped_providers, policy_notes).
    """
    if not policy_name or policy_name == "runway":
        return order, [], []
    
    policies = [p.strip() for p in policy_name.split(",")]
    skipped = []
    notes = []
    
    for pol in policies:
        if pol == "cost-cap":
            cap = policy_config.get("cost_max_per_1m", 5.0)  # default $5/M
            filtered = []
            for p in order:
                model = FLAGSHIP.get(p)
                cost = MODEL_COST_PER_1M_IN.get(model)
                if cost is not None and cost <= cap:
                    filtered.append(p)
                elif cost is not None:
                    skipped.append(p)
                    notes.append(f"cost-cap: {p}/{model} (${cost:.2f}/M > ${cap:.2f}/M cap)")
            order = filtered if filtered else order  # keep all if none qualify
            
        elif pol == "cost-prefer-cheapest":
            def cost_sort(p):
                cost = MODEL_COST_PER_1M_IN.get(FLAGSHIP.get(p), 999)
                return cost
            order = sorted(order, key=cost_sort)
            notes.append(f"cost-prefer-cheapest: order={[FLAGSHIP[p] for p in order]}")
            
        elif pol == "latency-weighted":
            def latency_sort(p):
                tier = MODEL_LATENCY_TIER.get(FLAGSHIP.get(p), 5)
                # Weight days_left by latency: faster models get a bonus
                return rw[p]["days_left"] / (1 + tier * 0.2)
            order = sorted(order, key=latency_sort, reverse=True)
            notes.append(f"latency-weighted: penalized slow models")
            
        elif pol == "quality-floor":
            floor = policy_config.get("quality_min_score", 70)
            filtered = []
            for p in order:
                score = MODEL_QUALITY_SCORE.get(FLAGSHIP.get(p), 0)
                if score >= floor:
                    filtered.append(p)
                else:
                    skipped.append(p)
                    notes.append(f"quality-floor: {p}/{FLAGSHIP[p]} (score {score} < {floor})")
            order = filtered if filtered else order
            
        elif pol == "cost-cap,quality-floor" or pol == "cost-cap+quality-floor":
            # Handled by comma splitting above — two passes
            pass
    
    return order, skipped, notes


def runway():
    """Pull per-provider days_left + balance from plutus.py --json."""
    out = subprocess.run([sys.executable, PLUTUS, "--json"],
                         capture_output=True, text=True)
    data = json.loads(out.stdout)
    rw = {}
    for e in data["providers"]:
        p = e["provider"]
        if p not in PROVIDERS:
            continue
        dl = e.get("days_left")
        # None days_left = no burn / unknown -> treat as very high runway
        rw[p] = {
            "days_left": dl if dl is not None else 1e9,
            "balance": e.get("balance"),
            "remaining": e.get("remaining"),
            "burn_per_day": e.get("burn_per_day"),
        }
    # ensure all three present
    for p in PROVIDERS:
        rw.setdefault(p, {"days_left": 1e9, "balance": None,
                          "remaining": None, "burn_per_day": 0})
    return rw, data


def load_yaml(path):
    import yaml
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def plan(rw, policy=None, policy_config=None):
    # highest runway first (base order)
    order = sorted(PROVIDERS, key=lambda p: rw[p]["days_left"], reverse=True)
    skipped = []
    notes = []
    
    # Apply routing policy if configured
    if policy and policy != "runway":
        order, skipped, notes = _apply_policy(order, rw, policy, policy_config or {})
    
    # Only available providers become primary/delegation
    available = [p for p in order if p not in skipped]
    if not available:
        available = order  # fall back to runway if all skipped
    
    primary = available[0]
    others = available[1:]
    # delegation: fast model of the best non-primary provider
    deleg_provider = others[0] if others else order[-1]  # last resort
    fallbacks = []
    for p in others:
        fallbacks.append((p, FLAGSHIP[p]))   # capable fallback
    for p in others:
        fallbacks.append((p, SUBTASK[p]))    # light subtask fallback
    
    result = {
        "order": order,
        "primary": primary,
        "primary_model": FLAGSHIP[primary],
        "delegation_provider": deleg_provider,
        "delegation_model": SUBTASK[deleg_provider],
        "fallbacks": fallbacks,
    }
    if skipped:
        result["skipped"] = skipped
    if notes:
        result["policy_notes"] = notes
    return result


def apply(cfg_path, p, providers_cfg, dry=False):
    import yaml
    cfg = load_yaml(cfg_path)
    pre_keys = set(cfg.keys())
    pre_provs = set((cfg.get("providers") or {}).keys())

    def pcfg(name):
        c = providers_cfg[name]
        return {"base_url": c.get("base_url"), "api_key": c.get("api_key")}

    # --- primary ---
    prim = pcfg(p["primary"])
    cfg["model"]["default"] = f"{p['primary']}/{p['primary_model']}" \
        if p["primary"] != "deepseek" else p["primary_model"]
    cfg["model"]["provider"] = p["primary"]
    # keep provider block's base_url/api_key authoritative; set top-level provider only

    # --- fallbacks ---
    fb = []
    for prov, model in p["fallbacks"]:
        c = pcfg(prov)
        entry = {"provider": prov, "model": model,
                 "base_url": c["base_url"], "api_key": c["api_key"]}
        if prov == "anthropic":
            entry["api_mode"] = "anthropic_messages"
            entry["context_length"] = 200000
        else:
            entry["context_length"] = providers_cfg[prov].get("context_length", 1048576)
        fb.append(entry)
    cfg["fallback_providers"] = fb

    # --- delegation (subtask model) ---
    dc = pcfg(p["delegation_provider"])
    cfg.setdefault("delegation", {})
    cfg["delegation"]["provider"] = p["delegation_provider"]
    cfg["delegation"]["model"] = p["delegation_model"]
    cfg["delegation"]["base_url"] = dc["base_url"]
    cfg["delegation"]["api_key"] = dc["api_key"]

    # --- VERIFY before writing: no top-level keys or provider blocks lost ---
    post_keys = set(cfg.keys())
    post_provs = set((cfg.get("providers") or {}).keys())
    if pre_keys - post_keys:
        raise RuntimeError(f"REFUSING WRITE: top-level keys would be lost: {pre_keys-post_keys}")
    if pre_provs - post_provs:
        raise RuntimeError(f"REFUSING WRITE: provider blocks would be lost: {pre_provs-post_provs}")

    if dry:
        return cfg, None

    # backup then write
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{cfg_path}.plutus-bak-{ts}"
    shutil.copy2(cfg_path, backup)
    with open(cfg_path, "w", encoding='utf-8') as f:
        yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False, allow_unicode=True)

    # re-read & re-verify round-trip
    rt = load_yaml(cfg_path)
    assert set(rt.keys()) == post_keys, "post-write top-level key mismatch"
    assert set((rt.get("providers") or {}).keys()) == post_provs, "post-write provider mismatch"
    assert rt["model"]["provider"] == p["primary"], "primary not applied"
    return cfg, backup


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Plutus route — credit-aware model routing for Hermes")
    ap.add_argument("--version", action="version", version=f"plutus v{VERSION}")
    ap.add_argument("--dry-run", action="store_true", help="preview routing without writing config")
    ap.add_argument("--apply", action="store_true", help="write routing to config.yaml")
    ap.add_argument("--policy", metavar="NAME", help="override routing policy (runway, cost-cap, latency-weighted, quality-floor, or comma-separated stack)")
    args = ap.parse_args()
    dry = args.dry_run
    rw, data = runway()
    providers_cfg = load_yaml(CONFIG).get("providers", {})
    
    # Load policy: CLI --policy overrides config
    policy_name = args.policy or _load_policy_config()
    policy_config = {}
    budgets_path = os.environ.get("PLUTUS_BUDGETS", os.path.join(HERE, "plutus.budgets.json"))
    try:
        if os.path.exists(budgets_path):
            cfg = json.load(open(budgets_path, encoding='utf-8'))
            policy_config = cfg.get("routing", {})
    except Exception:
        pass
    
    pl = plan(rw, policy=policy_name, policy_config=policy_config)

    print("Runway (days left):")
    for prov in pl["order"]:
        dl = rw[prov]["days_left"]
        dls = "∞" if dl >= 1e8 else f"{dl:.0f}"
        bal = rw[prov]["balance"]
        rem = rw[prov]["remaining"]
        amt = f"${bal:.2f} live" if bal is not None else (f"${rem:.2f} est" if rem is not None else "—")
        print(f"  {prov:10} {dls:>6} days   {amt}")
    print()
    print(f"POLICY      {policy_name}")
    if pl.get("skipped"):
        print(f"SKIPPED     {', '.join(pl['skipped'])}")
    if pl.get("policy_notes"):
        for note in pl["policy_notes"]:
            print(f"  {note}")
    print(f"PRIMARY     {pl['primary']} / {pl['primary_model']}")
    print(f"DELEGATION  {pl['delegation_provider']} / {pl['delegation_model']}  (subtasks)")
    print("FALLBACKS   " + " -> ".join(f"{p}/{m}" for p, m in pl["fallbacks"]))
    print()

    if not (dry or args.apply):
        print("No action. Re-run with --dry-run (preview write) or --apply (write config).")
        return

    cfg, backup = None, None
    # no-op guard: skip write if current config already matches the plan
    cur = load_yaml(CONFIG)
    cur_default = (cur.get("model") or {}).get("default")
    want_default = f"{pl['primary']}/{pl['primary_model']}" \
        if pl["primary"] != "deepseek" else pl["primary_model"]
    cur_deleg = (cur.get("delegation") or {}).get("model")
    cur_fb = []
    for f in (cur.get("fallback_providers") or []):
        if isinstance(f, dict):
            cur_fb.append(f"{f.get('provider')}/{f.get('model')}")
        elif isinstance(f, str) and '/' in f:
            cur_fb.append(f)
    want_fb = [f"{p}/{m}" for p, m in pl["fallbacks"]]
    already = (cur_default == want_default and cur_deleg == pl["delegation_model"]
               and cur_fb == want_fb)
    if already and not dry:
        print(f"No change — already routed to {want_default}. Skipping write.")
        return

    cfg, backup = apply(CONFIG, pl, providers_cfg, dry=dry)
    if dry:
        print("DRY RUN — config not written. Verification passed (no keys/providers lost).")
        print(f"  would set model.default = {cfg['model']['default']}")
    else:
        rec = {"t": round(time.time(), 1), "primary": pl["primary"],
               "primary_model": pl["primary_model"],
               "delegation": f"{pl['delegation_provider']}/{pl['delegation_model']}",
               "fallbacks": [f"{p}/{m}" for p, m in pl["fallbacks"]],
               "runway": {k: (None if v["days_left"] >= 1e8 else round(v["days_left"], 1))
                          for k, v in rw.items()}}
        with open(ROUTE_LOG, "a", encoding='utf-8') as f:
            f.write(json.dumps(rec) + "\n")
        print(f"APPLIED. Backup: {backup}")
        print(f"  model.default = {cfg['model']['default']}")
        print("  New sessions pick this up. Routing logged to plutus.routing.jsonl")


if __name__ == "__main__":
    main()
