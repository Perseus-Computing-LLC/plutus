"""Pricing — plan tiers, provider price tables, and token→cost math.

Two responsibilities:

1. **Plan tiers** (Free / Pro / Enterprise) — what a customer pays Plutus and
   what limits apply.
2. **Provider price tables** — public per-token prices for the upstream LLM
   providers, used to *estimate* the USD cost of a usage event when the caller
   doesn't supply an exact ``cost_usd``. These mirror how ``plutus.py`` prefers
   ``actual_cost_usd`` but falls back to ``estimated_cost_usd``.

Everything here is plain data + pure functions, so it is trivially testable and
fully offline. Prices are overridable via ``~/.plutus/config.yaml`` →
``pricing.overrides`` so they can be trued-up without a code change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------- plan tiers ---
@dataclass(frozen=True)
class Tier:
    key: str
    name: str
    price_usd_month: float
    tracked_tokens_month: Optional[int]  # None = unlimited
    workspaces: Optional[int]            # None = unlimited
    features: tuple = field(default_factory=tuple)
    blurb: str = ""

    @property
    def is_metered_limit(self) -> bool:
        return self.tracked_tokens_month is not None


TIERS = {
    "free": Tier(
        key="free",
        name="Free",
        price_usd_month=0.0,
        tracked_tokens_month=10_000,
        workspaces=1,
        features=(
            "10K tracked tokens / month",
            "1 workspace",
            "Live dashboard",
            "Community support",
        ),
        blurb="Track a single agent's spend. No card required.",
    ),
    "pro": Tier(
        key="pro",
        name="Pro",
        price_usd_month=20.0,
        tracked_tokens_month=None,
        workspaces=10,
        features=(
            "Unlimited tracked tokens",
            "Up to 10 workspaces",
            "Prepaid credits + auto-deplete",
            "Low-balance & budget-cap alerts",
            "Monthly PDF spend reports",
            "Stripe Checkout + Customer Portal",
        ),
        blurb="For solo builders and small teams running real agent workloads.",
    ),
    "enterprise": Tier(
        key="enterprise",
        name="Enterprise",
        price_usd_month=0.0,  # custom / contact sales
        tracked_tokens_month=None,
        workspaces=None,
        features=(
            "Everything in Pro",
            "Unlimited workspaces & seats",
            "SSO (SAML / OIDC)",
            "Custom budget policies & SLA",
            "Self-hosted or dedicated",
            "Priority support",
        ),
        blurb="Org-wide FinOps with custom limits, SSO, and an SLA.",
    ),
}

DEFAULT_TIER = "free"


def tier(key: str) -> Tier:
    return TIERS.get((key or DEFAULT_TIER).lower(), TIERS[DEFAULT_TIER])


# ----------------------------------------------------- provider price tables ---
# USD per 1,000,000 tokens. Public list prices, kept deliberately conservative
# and easy to override. (input, output, cache_read, reasoning) — reasoning is
# billed at the output rate unless a provider prices it separately.
#
# These are estimates used only when an exact cost isn't supplied. They are NOT
# a source of truth for what a provider charges you; calibrate against your
# console with the monitor's `--calibrate`, or pass exact `cost_usd` at meter
# time.
@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, reasoning_tokens: int = 0) -> float:
        return (
            input_tokens / 1_000_000 * self.input
            + (output_tokens + reasoning_tokens) / 1_000_000 * self.output
            + cache_read_tokens / 1_000_000 * self.cache_read
        )


# provider -> {model_id: ModelPrice}, plus a "_default" per provider.
PRICE_TABLE: dict[str, dict[str, ModelPrice]] = {
    "anthropic": {
        "_default": ModelPrice(3.0, 15.0, 0.30),
        "claude-opus-4-8": ModelPrice(15.0, 75.0, 1.50),
        "claude-sonnet-4-5-20250929": ModelPrice(3.0, 15.0, 0.30),
        "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30),
        "claude-haiku-4-5-20251001": ModelPrice(1.0, 5.0, 0.10),
    },
    "google": {
        "_default": ModelPrice(1.25, 5.0, 0.31),
        "gemini-3.1-pro-preview": ModelPrice(1.25, 10.0, 0.31),
        "gemini-2.5-flash": ModelPrice(0.30, 2.50, 0.075),
    },
    "deepseek": {
        "_default": ModelPrice(0.27, 1.10, 0.027),
        "deepseek-v4-pro": ModelPrice(0.55, 2.19, 0.055),
        "deepseek-v4-flash": ModelPrice(0.14, 0.28, 0.014),
    },
    "openai": {
        "_default": ModelPrice(2.50, 10.0, 1.25),
    },
    "_default": {
        "_default": ModelPrice(1.0, 3.0, 0.10),
    },
}


def model_price(provider: str, model: Optional[str] = None,
                overrides: Optional[dict] = None) -> ModelPrice:
    """Resolve the price for (provider, model), honoring config overrides.

    ``overrides`` is the optional ``pricing.overrides`` config block, shaped like
    ``{provider: {model: {input, output, cache_read}}}``.
    """
    provider = (provider or "_default").lower()
    if overrides and provider in overrides:
        table = overrides[provider]
        key = model if (model and model in table) else "_default"
        if key in table:
            p = table[key]
            return ModelPrice(
                float(p.get("input", 0)),
                float(p.get("output", 0)),
                float(p.get("cache_read", 0)),
            )
    prov = PRICE_TABLE.get(provider, PRICE_TABLE["_default"])
    if model and model in prov:
        return prov[model]
    return prov.get("_default", PRICE_TABLE["_default"]["_default"])


def estimate_cost(provider: str, model: Optional[str],
                  input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, reasoning_tokens: int = 0,
                  overrides: Optional[dict] = None) -> float:
    """Estimate USD cost of a usage event from token counts."""
    price = model_price(provider, model, overrides)
    return round(price.cost(input_tokens, output_tokens,
                            cache_read_tokens, reasoning_tokens), 6)
