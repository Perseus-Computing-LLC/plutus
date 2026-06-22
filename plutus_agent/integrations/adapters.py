"""Provider/framework adapters — normalize usage objects → Plutus meter calls."""
from __future__ import annotations

from typing import Any, Optional


def track_anthropic(meter, response: Any, *, model: Optional[str] = None,
                    task_type: str = "general", workspace: Optional[str] = None):
    """Meter an Anthropic Messages API response (``response.usage``).

        from anthropic import Anthropic
        from plutus_agent import Meter
        from plutus_agent.integrations import track_anthropic

        msg = Anthropic().messages.create(model="claude-opus-4-8", ...)
        track_anthropic(Meter(org="acme"), msg, task_type="code_review")
    """
    u = getattr(response, "usage", None) or {}
    get = (lambda k: getattr(u, k, None)) if not isinstance(u, dict) else u.get
    return meter.track(
        provider="anthropic",
        model=model or getattr(response, "model", None),
        task_type=task_type, workspace=workspace,
        input_tokens=int(get("input_tokens") or 0),
        output_tokens=int(get("output_tokens") or 0),
        cache_read_tokens=int(get("cache_read_input_tokens") or 0),
        source="anthropic",
    )


def track_openai(meter, response: Any, *, model: Optional[str] = None,
                 task_type: str = "general", workspace: Optional[str] = None):
    """Meter an OpenAI/compatible chat-completions response (``response.usage``)."""
    u = getattr(response, "usage", None) or {}
    get = (lambda k: getattr(u, k, None)) if not isinstance(u, dict) else u.get
    reasoning = 0
    details = get("completion_tokens_details")
    if details:
        reasoning = int((details.get("reasoning_tokens")
                         if isinstance(details, dict)
                         else getattr(details, "reasoning_tokens", 0)) or 0)
    return meter.track(
        provider="openai",
        model=model or getattr(response, "model", None),
        task_type=task_type, workspace=workspace,
        input_tokens=int(get("prompt_tokens") or 0),
        output_tokens=int(get("completion_tokens") or 0),
        reasoning_tokens=reasoning,
        source="openai",
    )


def track_hermes_session(meter, session: dict, *, workspace: Optional[str] = None):
    """Meter a row from Hermes ``state.db`` ``sessions`` (the format the original
    ``plutus.py`` reads). Prefers the exact ``actual_cost_usd`` when present.

        track_hermes_session(meter, {
            "billing_provider": "anthropic", "model": "claude-opus-4-8",
            "actual_cost_usd": 0.14, "input_tokens": 1200, "output_tokens": 800,
        }, workspace="hermes")
    """
    cost = session.get("actual_cost_usd") or session.get("estimated_cost_usd")
    return meter.track(
        provider=session.get("billing_provider") or "unknown",
        model=session.get("model"),
        task_type=session.get("task_type") or "agent",
        workspace=workspace,
        input_tokens=int(session.get("input_tokens") or 0),
        output_tokens=int(session.get("output_tokens") or 0),
        cache_read_tokens=int(session.get("cache_read_tokens") or 0),
        reasoning_tokens=int(session.get("reasoning_tokens") or 0),
        cost_usd=float(cost) if cost else None,
        source="hermes",
    )
