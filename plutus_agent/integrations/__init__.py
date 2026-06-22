"""Integration helpers — thin adapters that map common agent-framework usage
objects onto :class:`plutus_agent.Meter`.

These are intentionally tiny and dependency-free: they read token counts off
whatever usage object a provider SDK returns and forward them to Plutus. Import
the one you need, or copy the three lines into your own hot path.
"""
from .adapters import (
    track_anthropic,
    track_openai,
    track_hermes_session,
)

__all__ = ["track_anthropic", "track_openai", "track_hermes_session"]
