"""Configuration — ``~/.plutus/config.yaml`` load / save / defaults.

Plutus follows the original monitor's philosophy: sensible defaults, everything
overridable by env var, and config that never silently loses data. YAML is the
on-disk format (PyYAML), but if PyYAML is somehow unavailable we degrade to a
minimal built-in reader so ``plutus`` still runs.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------- locations ---
def home_dir() -> Path:
    """The Plutus home (``~/.plutus`` unless ``PLUTUS_HOME`` overrides)."""
    return Path(os.environ.get("PLUTUS_HOME", str(Path.home() / ".plutus")))


def config_path() -> Path:
    return Path(os.environ.get("PLUTUS_CONFIG", str(home_dir() / "config.yaml")))


def db_path() -> Path:
    return Path(os.environ.get("PLUTUS_DB", str(home_dir() / "plutus.db")))


# ----------------------------------------------------------------- defaults ---
DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8420,
    },
    "auth": {
        # Google OIDC sign-in. Disabled by default → the dashboard is open
        # (fine for localhost / behind a trusted proxy). Set ``enabled`` plus
        # the Google client creds (prefer env vars) to require login. Sign-in is
        # limited to people who are already members of an org, plus anyone in
        # ``allowed_emails`` or at ``allowed_domain``.
        "enabled": False,
        "google_client_id": "",       # or env PLUTUS_GOOGLE_CLIENT_ID
        "google_client_secret": "",   # or env PLUTUS_GOOGLE_CLIENT_SECRET
        "base_url": "",               # public origin, e.g. https://plutus.perseus.observer
        "allowed_emails": [],         # extra emails allowed to sign in
        "allowed_domain": "",         # e.g. "perseus.observer" — any address here may sign in
        "provision_org_id": "",       # if set, a newly-allowed email joins this org as 'member'
        "allow_signup": False,        # OPEN signup: any verified Google account gets
                                      # its own new Free-tier org (self-serve SaaS). Off
                                      # by default so a private instance stays allow-listed.
        "session_ttl_hours": 168,     # session lifetime (7 days)
    },
    "billing": {
        # Stripe is optional. Leave keys empty to run fully offline; the
        # dashboard shows billing in "test/offline" mode and Checkout is
        # disabled until a key is present. Prefer env vars over file for keys.
        "stripe_secret_key": "",        # or env STRIPE_SECRET_KEY
        "stripe_publishable_key": "",   # or env STRIPE_PUBLISHABLE_KEY
        "stripe_webhook_secret": "",    # or env STRIPE_WEBHOOK_SECRET
        "stripe_price_pro": "",         # Price ID for the $20/mo Pro plan
        "currency": "usd",
        "success_url": "http://localhost:8420/billing/success",
        "cancel_url": "http://localhost:8420/billing/cancel",
    },
    "alerts": {
        "enabled": False,
        "low_balance_usd": 10.0,        # warn when org credit drops below this
        "budget_warn_pct": 80.0,        # warn when workspace hits this % of cap
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",            # or env PLUTUS_SMTP_PASSWORD
        "from_addr": "plutus@perseus.observer",
        "to_addrs": [],
    },
    "monitor": {
        # Optional bridge to the live runway monitor (repo-root plutus.py).
        # When set, the dashboard folds in live provider balances/runway.
        "enabled": False,
        "command": "",                  # e.g. "python3 /opt/.../plutus.py"
    },
    "pricing": {
        # Override provider price tables here, shaped:
        # overrides: { anthropic: { claude-opus-4-8: {input: 15, output: 75} } }
        "overrides": {},
        # Free-tier quota: when an org on a limited tier exceeds its monthly
        # tracked-token allowance, events are still recorded but flagged
        # ``over_free_limit`` so the dashboard can nudge an upgrade. Flip
        # ``block_over_free_limit`` on to HARD-stop recording past the cap
        # (returns a non-recorded result) — off by default so no billing data
        # is ever silently dropped.
        "block_over_free_limit": False,
        # Prepaid credit hard-stop: when enabled and an org has prepaid credit,
        # events that would push the balance negative are rejected (not recorded).
        "block_over_balance": False,
    },
}


# ------------------------------------------------------------------- yaml io ---
def _load_yaml(path: Path) -> dict:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return _minimal_yaml_read(path)
    except FileNotFoundError:
        return {}
    except Exception as e:  # pragma: no cover - corrupt file
        import sys
        sys.stderr.write(f"plutus: could not read config {path}: {e}\n")
        return {}


def _dump_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False,
                           allow_unicode=True)
    except ImportError:  # pragma: no cover - PyYAML is a declared dep
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def _minimal_yaml_read(path: Path) -> dict:
    """Tiny fallback reader (flat key: value, one level of nesting by indent).

    Only used if PyYAML is missing. Good enough to read a config we wrote.
    """
    try:
        import json
        text = path.read_text(encoding="utf-8")
        # we may have written JSON as a fallback
        if text.lstrip().startswith("{"):
            return json.loads(text)
    except Exception:
        pass
    return {}


# ----------------------------------------------------------------- merging ---
def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_base() -> dict:
    """Defaults merged with the on-disk config only — **no environment**.

    This is what may be written back with :func:`save`. Env-provided secrets
    (Stripe keys, SMTP password) are deliberately excluded so they never get
    persisted to ``config.yaml`` in plaintext.
    """
    return _deep_merge(DEFAULT_CONFIG, _load_yaml(config_path()))


def load() -> dict:
    """Runtime config, layering: defaults < file < environment overrides.

    Use for *reading* config at runtime. Do NOT pass the result to
    :func:`save` — that would persist env-injected secrets. Use
    :func:`load_base` as the basis for anything you intend to save.
    """
    cfg = load_base()

    # environment overrides (keys never logged, never saved)
    env = os.environ
    if env.get("STRIPE_SECRET_KEY"):
        cfg["billing"]["stripe_secret_key"] = env["STRIPE_SECRET_KEY"]
    if env.get("STRIPE_PUBLISHABLE_KEY"):
        cfg["billing"]["stripe_publishable_key"] = env["STRIPE_PUBLISHABLE_KEY"]
    if env.get("STRIPE_WEBHOOK_SECRET"):
        cfg["billing"]["stripe_webhook_secret"] = env["STRIPE_WEBHOOK_SECRET"]
    if env.get("STRIPE_PRICE_PRO"):
        cfg["billing"]["stripe_price_pro"] = env["STRIPE_PRICE_PRO"]
    if env.get("PLUTUS_SMTP_PASSWORD"):
        cfg["alerts"]["smtp_password"] = env["PLUTUS_SMTP_PASSWORD"]
    if env.get("PLUTUS_PORT"):
        try:
            cfg["server"]["port"] = int(env["PLUTUS_PORT"])
        except ValueError:
            pass

    # auth / OIDC overrides
    if env.get("PLUTUS_AUTH_ENABLED"):
        cfg["auth"]["enabled"] = env["PLUTUS_AUTH_ENABLED"].strip().lower() in (
            "1", "true", "yes", "on")
    if env.get("PLUTUS_GOOGLE_CLIENT_ID"):
        cfg["auth"]["google_client_id"] = env["PLUTUS_GOOGLE_CLIENT_ID"]
    if env.get("PLUTUS_GOOGLE_CLIENT_SECRET"):
        cfg["auth"]["google_client_secret"] = env["PLUTUS_GOOGLE_CLIENT_SECRET"]
    if env.get("PLUTUS_BASE_URL"):
        cfg["auth"]["base_url"] = env["PLUTUS_BASE_URL"]
    if env.get("PLUTUS_ALLOWED_EMAILS"):
        cfg["auth"]["allowed_emails"] = [
            e.strip() for e in env["PLUTUS_ALLOWED_EMAILS"].split(",") if e.strip()]
    if env.get("PLUTUS_ALLOWED_DOMAIN"):
        cfg["auth"]["allowed_domain"] = env["PLUTUS_ALLOWED_DOMAIN"]
    if env.get("PLUTUS_ALLOW_SIGNUP"):
        cfg["auth"]["allow_signup"] = env["PLUTUS_ALLOW_SIGNUP"].strip().lower() in (
            "1", "true", "yes", "on")
    return cfg


def _strip_env_secrets(cfg: dict) -> dict:
    """Return a deep-ish copy with any secret that matches its env var blanked,
    so secrets sourced from the environment are never written to disk."""
    import copy
    out = copy.deepcopy(cfg)
    env = os.environ
    pairs = [
        ("billing", "stripe_secret_key", "STRIPE_SECRET_KEY"),
        ("billing", "stripe_publishable_key", "STRIPE_PUBLISHABLE_KEY"),
        ("billing", "stripe_webhook_secret", "STRIPE_WEBHOOK_SECRET"),
        ("alerts", "smtp_password", "PLUTUS_SMTP_PASSWORD"),
        ("auth", "google_client_secret", "PLUTUS_GOOGLE_CLIENT_SECRET"),
    ]
    for section, key, envvar in pairs:
        val = out.get(section, {}).get(key)
        if val and env.get(envvar) and val == env[envvar]:
            out[section][key] = ""
    return out


def save(cfg: dict) -> Path:
    """Persist config to disk. Secrets that came from the environment are
    stripped first (see :func:`_strip_env_secrets`) — ``config.yaml`` should
    never hold a live key that was provided via env."""
    path = config_path()
    _dump_yaml(path, _strip_env_secrets(cfg))
    return path


def ensure_initialized() -> tuple[Path, bool]:
    """Create ``~/.plutus/config.yaml`` from defaults if it doesn't exist.

    Returns (path, created).
    """
    path = config_path()
    if path.exists():
        return path, False
    save(DEFAULT_CONFIG)
    return path, True


def stripe_enabled(cfg: dict) -> bool:
    return bool(cfg.get("billing", {}).get("stripe_secret_key"))


def auth_enabled(cfg: dict) -> bool:
    """True only when login is both turned on AND fully configured.

    If ``auth.enabled`` is set but the Google client creds are missing we treat
    auth as *off* rather than locking everyone out of a misconfigured server.
    """
    a = cfg.get("auth", {})
    return bool(a.get("enabled") and a.get("google_client_id")
                and a.get("google_client_secret"))
