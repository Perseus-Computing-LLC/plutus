"""Plutus — the billing layer for AI agents.

Self-hosted, Stripe-integrated usage metering and prepaid-credit billing for
LLM / AI-agent spend. Multi-tenant (organizations → workspaces → users), meters
usage per workspace / provider / task-type, depletes prepaid credits as calls
route through, and serves a dark-themed real-time dashboard at :8420.

Everything except Stripe works fully offline. State lives in SQLite
(``~/.plutus/plutus.db`` by default); configuration in ``~/.plutus/config.yaml``.

This package is the *monetization engine*. The original credit monitor and
runway router (``plutus.py`` / ``plutus_route.py`` at the repo root) remain the
live FinOps tools; the engine bridges to them via ``plutus_agent.bridge`` rather
than importing them, so the two can ship and run independently.
"""

__version__ = "0.4.0"
__product__ = "Plutus"
__tagline__ = "The billing layer for AI agents."
__company__ = "Perseus Computing LLC"
__homepage__ = "https://perseus.observer/plutus/"
__default_port__ = 8420

__all__ = [
    "__version__",
    "__product__",
    "__tagline__",
    "__default_port__",
    "Meter",
]


def __getattr__(name):
    # Lazy export so `from plutus_agent import Meter` works without importing
    # the world at package-load time.
    if name == "Meter":
        from .client import Meter
        return Meter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
