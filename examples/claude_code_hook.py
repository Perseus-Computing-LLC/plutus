"""Claude Code / Codex CLI → Plutus, via a Stop hook.

Claude Code can run a command when a turn finishes (a `Stop` hook). Point it at
this script to meter each turn's token usage into Plutus. Codex CLI works the
same way — pipe its usage JSON to stdin.

settings.json (Claude Code):
    {
      "hooks": {
        "Stop": [{ "hooks": [
          { "type": "command",
            "command": "python /path/to/examples/claude_code_hook.py" }
        ]}]
      }
    }

The hook receives JSON on stdin. We read token counts from it (best-effort,
since the exact shape varies by version) and meter the turn. Unknown fields are
ignored; missing usage just records a zero-cost event so call counts stay right.
"""
import json
import sys

from plutus_agent import Meter


def _dig(d, *keys, default=0):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d or default


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    usage = payload.get("usage") or _dig(payload, "message", "usage", default={}) or {}
    model = payload.get("model") or _dig(payload, "message", "model", default="claude")
    workspace = payload.get("cwd") or payload.get("workspace") or "claude-code"
    # normalize the workspace to a short slug-ish name
    workspace = str(workspace).rstrip("/").rsplit("/", 1)[-1] or "claude-code"

    meter = Meter(org="Claude Code", tier="pro")
    res = meter.track(
        provider="anthropic",
        model=model,
        task_type=payload.get("task_type", "coding"),
        workspace=workspace,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        source="claude-code",
    )
    meter.close()
    # Hooks should be quiet on success; surface only alerts.
    for a in res.alerts:
        print(f"[plutus] {a['kind']}: {a['message']}", file=sys.stderr)


if __name__ == "__main__":
    main()
