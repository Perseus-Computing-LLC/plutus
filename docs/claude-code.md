# See where your Claude Code spend actually goes — in 30 seconds

Claude Code, Codex, and agent CLIs burn tokens fast, and you get one number on a
bill at the end of the month with no idea *which project* or *what kind of work*
cost what. Plutus fixes that with a one-command hook.

## Install

```bash
pip install plutus-agent
plutus install-claude-hook
```

That merges a `Stop` hook into `~/.claude/settings.json`. From now on, **every
Claude Code turn meters into Plutus automatically** — attributed to the project
you're working in (the hook uses your cwd's name as the workspace).

```bash
# ...do some coding with Claude Code...
plutus serve          # → http://localhost:8420
```

You'll see spend per project, per task type, cost-per-turn, and a live feed —
the breakdown your monthly bill never gives you.

## What the hook does

It reads the turn's token usage from the JSON Claude Code pipes to the hook and
records one metered event. It's quiet on success (hooks should be), surfaces
only low-balance/budget alerts to stderr, and **never breaks your session** —
any error is swallowed.

- Attribute to a specific org: `export PLUTUS_ORG="My Team"`.
- Set a monthly cap per project and get alerted before you blow it:
  `plutus workspace create my-project --budget 50`.
- Add prepaid credit so spend depletes a balance you control:
  `plutus topup --amount 25` (or buy it via Stripe — see [BILLING.md](../BILLING.md)).

## Codex CLI / other agents

Any tool that can run a command on completion works the same way — pipe its
usage JSON to:

```bash
python -m plutus_agent.integrations.claude_code_hook
```

Print the exact snippet for manual wiring with `plutus install-claude-hook --print`.

## Uninstall

Remove the `Stop` entry from `~/.claude/settings.json` (a `.plutus-bak` backup
was written next to it when you installed).
