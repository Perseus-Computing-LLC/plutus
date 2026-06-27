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
# Estimates below are public list prices as of this date. They drift; calibrate
# or pass exact cost_usd. Surfaced on the pricing page so users see the vintage.
PRICE_TABLE_AS_OF = "2026-06-26"


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float = 0.0
    # Per-1M rate for reasoning/"thinking" tokens. None => billed at the output
    # rate (the common case; most providers don't price reasoning separately).
    reasoning: Optional[float] = None

    def cost(self, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, reasoning_tokens: int = 0) -> float:
        reasoning_rate = self.output if self.reasoning is None else self.reasoning
        return (
            input_tokens / 1_000_000 * self.input
            + output_tokens / 1_000_000 * self.output
            + reasoning_tokens / 1_000_000 * reasoning_rate
            + cache_read_tokens / 1_000_000 * self.cache_read
        )


# provider -> {model_id: ModelPrice}, plus a "_default" per provider. Prices are
# USD per 1,000,000 tokens (input, output, cache_read). See PRICE_TABLE_AS_OF.
# These are *estimates* for events metered without an exact cost_usd — not a
# source of truth — and any model not matched here is flagged `unpriced` (see
# resolve_price) so a fallback estimate is never mistaken for an exact price.
PRICE_TABLE: dict[str, dict[str, ModelPrice]] = {
    "anthropic": {
        "_default": ModelPrice(3.0, 15.0, 0.30),
        "claude-fable-5": ModelPrice(15.0, 75.0, 1.50),
        "claude-opus-4-8": ModelPrice(15.0, 75.0, 1.50),
        "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 0.30),
        "claude-sonnet-4-5-20250929": ModelPrice(3.0, 15.0, 0.30),
        "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30),
        "claude-haiku-4-5-20251001": ModelPrice(1.0, 5.0, 0.10),
        "claude-haiku-4-5": ModelPrice(1.0, 5.0, 0.10),
    },
    "openai": {
        "_default": ModelPrice(2.50, 10.0, 1.25),
        "gpt-5": ModelPrice(1.25, 10.0, 0.125),
        "gpt-5-mini": ModelPrice(0.25, 2.0, 0.025),
        "gpt-5-nano": ModelPrice(0.05, 0.40, 0.005),
        "o4": ModelPrice(2.50, 10.0, 0.625),
        "o4-mini": ModelPrice(1.10, 4.40, 0.275),
    },
    "google": {
        "_default": ModelPrice(1.25, 5.0, 0.31),
        "gemini-3.1-pro-preview": ModelPrice(1.25, 10.0, 0.31),
        "gemini-3.1-pro": ModelPrice(1.25, 10.0, 0.31),
        "gemini-2.5-pro": ModelPrice(1.25, 10.0, 0.31),
        "gemini-2.5-flash": ModelPrice(0.30, 2.50, 0.075),
    },
    "deepseek": {
        "_default": ModelPrice(0.27, 1.10, 0.027),
        "deepseek-v4-pro": ModelPrice(0.55, 2.19, 0.055),
        "deepseek-v4-flash": ModelPrice(0.14, 0.28, 0.014),
    },
    "xai": {
        "_default": ModelPrice(3.0, 15.0, 0.75),
        "grok-4": ModelPrice(3.0, 15.0, 0.75),
        "grok-4-fast": ModelPrice(0.20, 0.50, 0.05),
    },
    "mistral": {
        "_default": ModelPrice(2.0, 6.0, 0.0),
        "mistral-large-2": ModelPrice(2.0, 6.0, 0.0),
        "mistral-small-3": ModelPrice(0.20, 0.60, 0.0),
    },
    "cohere": {
        "_default": ModelPrice(2.50, 10.0, 0.0),
        "command-a": ModelPrice(2.50, 10.0, 0.0),
    },
    "meta": {
        "_default": ModelPrice(0.35, 0.40, 0.0),
        "llama-4-maverick": ModelPrice(0.35, 1.15, 0.0),
        "llama-4-scout": ModelPrice(0.11, 0.34, 0.0),
    },
    "_default": {
        "_default": ModelPrice(1.0, 3.0, 0.10),
    },
}


def resolve_price(provider: str, model: Optional[str] = None,
                  overrides: Optional[dict] = None) -> tuple[ModelPrice, bool]:
    """Resolve ``(price, exact)`` for (provider, model), honoring config overrides.

    ``exact`` is ``True`` only when the specific model was matched (in overrides
    or the table). It is ``False`` whenever we fall back to a provider ``_default``
    or the global default, or when no model was supplied — i.e. the cost is a
    coarse estimate the caller should treat as ``unpriced``.

    ``overrides`` is the optional ``pricing.overrides`` config block, shaped like
    ``{provider: {model: {input, output, cache_read[, reasoning]}}}``.
    """
    provider = (provider or "_default").lower()

    def _from_override(p: dict) -> ModelPrice:
        r = p.get("reasoning")
        return ModelPrice(
            float(p.get("input", 0)),
            float(p.get("output", 0)),
            float(p.get("cache_read", 0)),
            None if r is None else float(r),
        )

    if overrides and provider in overrides:
        table = overrides[provider]
        if model and model in table:
            return _from_override(table[model]), True
        if "_default" in table:
            return _from_override(table["_default"]), False

    prov = PRICE_TABLE.get(provider)
    if prov is not None:
        if model and model in prov:
            return prov[model], True
        return prov.get("_default", PRICE_TABLE["_default"]["_default"]), False
    return PRICE_TABLE["_default"]["_default"], False


def model_price(provider: str, model: Optional[str] = None,
                overrides: Optional[dict] = None) -> ModelPrice:
    """Resolve the price for (provider, model). See :func:`resolve_price` for the
    matched/fallback distinction."""
    return resolve_price(provider, model, overrides)[0]


def estimate_cost(provider: str, model: Optional[str],
                  input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, reasoning_tokens: int = 0,
                  overrides: Optional[dict] = None) -> float:
    """Estimate USD cost of a usage event from token counts."""
    price = model_price(provider, model, overrides)
    return round(price.cost(input_tokens, output_tokens,
                            cache_read_tokens, reasoning_tokens), 6)
