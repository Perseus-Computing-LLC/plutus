# Plutus

> Named for the Greek god of wealth. Plutus watches the money draining out of every LLM provider you use — and automatically balances your model routing toward the provider with the most runway.

Plutus is a credit & spend monitor for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that does two things:

1. **Monitors** — shows live balance, spend (today / 7d / 30d / all-time), burn rate, and projected days-left for every provider you care about, in one table.
2. **Balances** — ranks providers by runway and rewrites Hermes model routing so the provider with the most credit runs its flagship as primary, while the others supply the best subtask/fallback models.

## Why

If you run multiple LLM providers (DeepSeek, Anthropic, Google, ...) you're juggling separate billing consoles and have no single view of where your money is going or which provider is about to run dry. Plutus gives you that view and acts on it.

## How it works

Two data sources, fused per provider:

| Source | Used for |
|---|---|
| **Live balance API** | Providers that expose one (DeepSeek: `GET /user/balance`). Real-time dollars. |
| **Local spend ledger** | Everyone else. Read from Hermes `state.db` (`sessions.billing_provider`, `estimated_cost_usd`/`actual_cost_usd`, token counts). Gives spend + burn rate. |

For providers without a balance API (Anthropic, Google), you supply a starting **budget** and Plutus computes `remaining = budget - ledger_spend`. A `--calibrate` command lets you periodically true it up against the real console number — it back-solves the budget so estimates self-correct.

## Usage

```bash
# Monitor
python3 plutus.py                 # pretty CLI table
python3 plutus.py --json          # machine-readable
python3 plutus.py --html out.html # dashboard
python3 plutus.py --snapshot      # append burn-rate history

# Calibrate a no-balance-API provider against its real console balance
python3 plutus.py --calibrate anthropic=74.46 --calibrate google=93.59

# Balance model routing by runway
python3 plutus_route.py --dry-run # preview the routing decision (no write)
python3 plutus_route.py --apply   # rewrite Hermes config.yaml routing
```

## Routing policy

`plutus_route.py` ranks providers by projected days-left, then:

- **Primary** = flagship model of the highest-runway provider
- **Delegation** (subtask model) = fast model of the best non-primary provider
- **Fallbacks** = the other two providers, flagship first, fast model second

Every config write is **backed up first**, **refuses to write if any provider block or API key would be lost**, and **re-verifies the round-trip** after writing. A no-op guard skips the write entirely when the routing decision hasn't changed.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `PLUTUS_HERMES_CONFIG` | `/opt/data/webui/minions-hermes-config/config.yaml` | Hermes config (provider keys + routing) |
| `PLUTUS_STATE_DB` | `.../minions-hermes-config/state.db` | Hermes session/spend ledger |
| `PLUTUS_BUDGETS` | `./plutus.budgets.json` | Per-provider starting budgets |
| `PLUTUS_PROVIDERS` | `deepseek,anthropic,google` | Which providers to track, in order |
| `PLUTUS_SNAPSHOTS` | `./plutus.snapshots.jsonl` | Burn-rate history file |

Copy `plutus.budgets.example.json` to `plutus.budgets.json` and set your real numbers.

## Automation

Plutus is designed to run on a schedule (e.g. a Hermes cron job). The included `plutus-refresh.sh` regenerates the dashboard, appends a history snapshot, and re-runs the balancer every hour. A separate every-3-days job can prompt you to confirm the real Anthropic/Google balances and recalibrate.

## Files

| File | Purpose |
|---|---|
| `plutus.py` | The monitor (balance + spend + runway + calibrate) |
| `plutus_route.py` | The balancing arm (runway-based routing) |
| `plutus-refresh.sh` | Cron driver: refresh dashboard + rebalance |
| `plutus.budgets.example.json` | Template for per-provider budgets |

## Adding a live-balance provider

Add a fetcher function in `plutus.py` returning `{"balance_usd": ..., "ok": True}` and register it in `BALANCE_FETCHERS` plus `LEDGER_ALIASES`.

## License

MIT — see [LICENSE](LICENSE).
