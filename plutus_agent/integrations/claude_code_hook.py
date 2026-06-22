"""Claude Code / Codex CLI → Plutus, as a packaged Stop hook.

This is the canonical, installed version of the hook (the loose copy in
``examples/`` mirrors it). Claude Code runs a ``Stop`` hook command when a turn
finishes and pipes JSON on stdin; this reads the turn's token usage and meters
it into Plutus, so every coding turn shows up live on the dashboard and depletes
prepaid credit.

Wire it with ``plutus install-claude-hook`` (which merges the entry below into
``~/.claude/settings.json``), or by hand:

    {"hooks": {"Stop": [{"hooks": [
      {"type": "command",
       "command": "python -m plutus_agent.integrations.claude_code_hook"}
    ]}]}}

The org is ``$PLUTUS_ORG`` (default "Claude Code"); the workspace is the basename
of the turn's cwd, so spend is attributed per project automatically. Hooks must
be quiet on success — only alerts go to stderr.
"""
from __future__ import annotations

import json
import os
import sys


def _dig(d, *keys, default=0):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d if d is not None else default


def meter_payload(payload: dict):
    from plutus_agent import Meter

    usage = payload.get("usage") or _dig(payload, "message", "usage", default={}) or {}
    model = payload.get("model") or _dig(payload, "message", "model", default="claude")
    workspace = (payload.get("cwd") or payload.get("workspace")
                 or os.getcwd() or "claude-code")
    workspace = str(workspace).rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or "claude-code"

    meter = Meter(org=os.environ.get("PLUTUS_ORG", "Claude Code"), tier="pro")
    try:
        res = meter.track(
            provider=payload.get("provider", "anthropic"),
            model=model,
            task_type=payload.get("task_type", "coding"),
            workspace=workspace,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens")
                                  or usage.get("cache_read_tokens") or 0),
            source="claude-code",
        )
    finally:
        meter.close()
    return res


def main(argv=None):
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    try:
        res = meter_payload(payload)
    except Exception as e:  # a hook must never break the host tool
        sys.stderr.write(f"[plutus] hook error (non-fatal): {e}\n")
        return 0
    for a in res.alerts:
        sys.stderr.write(f"[plutus] {a['kind']}: {a['message']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
