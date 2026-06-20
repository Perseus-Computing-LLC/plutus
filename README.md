# Plutus — LLM FinOps Tool

[![Test](https://github.com/Perseus-Computing-LLC/plutus/actions/workflows/test.yml/badge.svg)](https://github.com/Perseus-Computing-LLC/plutus/actions/workflows/test.yml)

> Named for the Greek god of wealth. Plutus is a standalone provider credit monitor and cost-aware model router for multi-provider LLM stacks.

```
  ____  _       _
 |  _ \| |_   _| |_ _   _ ___
 | |_) | | | | | __| | | / __|   god of wealth
 |  __/| | |_| | |_| |_| \__ \   provider credit monitor
 |_|   |_|\__,_|\__|\__,_|___/
  generated 2026-06-20 09:08:00

PROVIDER        BALANCE     REMAIN     TODAY        7D       30D        ALL    $/DAY   DAYS SRC
-----------------------------------------------------------------------------------------------
deepseek         $49.74          —    $50.51    $99.88   $198.91    $198.91   $14.27      4 live
anthropic             —          —    $55.73    $55.73    $55.73     $55.73    $7.96      ∞ est
google                —          —     $0.01     $0.15     $0.15      $0.15    $0.02      ∞ est
openai                —          —     $0.00     $0.00     $0.00      $0.00    $0.00      ∞ est
-----------------------------------------------------------------------------------------------
TOTAL                                $106.24   $155.76   $254.79    $254.79
```

## What Plutus does

Plutus is an **LLM FinOps tool** — it tracks your spend across every LLM provider and balances routing to maximize credit runway:

| Capability | Description |
|---|---|
| **Live balance monitoring** | Pulls real balance from provider APIs (DeepSeek, OpenAI) |
| **Spend tracking** | 7-day, 30-day, and all-time burn rates per provider |
| **Budget estimation** | For providers without balance APIs (Anthropic, Google, custom), tracks remaining against declared budgets |
| **Runway forecasting** | Projects days-until-exhaustion and exhaustion dates |
| **Cost-aware routing** | Rewrites model routing to favor providers with the most runway, with optional cost-cap/latency/quality policies |
| **Multi-channel alerts** | Email (Himalaya), Discord webhook, ntfy.sh — configurable thresholds |
| **HTML dashboard** | Dark-themed dashboard with SVG sparkline trend lines |
| **Backtest mode** | Replay session history against routing policies to measure savings before deploying |

## Quick start

```bash
# Install
pip install plutus

# Monitor your providers
plutus

# Forecast budget exhaustion
plutus forecast

# Set up budgets for providers without balance APIs
plutus --calibrate anthropic=74.46 --calibrate google=93.59

# Route your model stack by runway
plutus-route --dry-run
plutus-route --apply

# Backtest a routing policy before deploying
plutus-route --backtest cost-cap
```

## How it works

Plutus reads your session history from a SQLite state database (Hermes Agent `state.db` by default, configurable) and fuses it with live balance APIs:

| Source | Used for |
|---|---|
| **Live balance API** | DeepSeek, OpenAI — real-time dollar balances via REST |
| **Spend ledger** | All providers — per-session cost rows (actual or estimated), token counts |
| **Declared budgets** | Providers without balance APIs — you set a starting budget, Plutus tracks remaining |

For estimate-based providers, `--calibrate` back-solves the budget from your real console balance and all-time ledger spend, then projects remaining going forward. Self-correcting over time.

## Routing policies

`plutus-route` supports configurable routing policies beyond pure runway:

| Policy | Behavior |
|---|---|
| `runway` (default) | Max days-left wins |
| `cost-cap` | Hard ceiling per 1M tokens, prefer cheapest under cap |
| `latency-weighted` | Prefer faster models, penalize slow ones |
| `quality-floor` | Filter out models below a benchmark score |
| Stacked | Comma-separated, e.g. `cost-cap,quality-floor` |

Configure in `plutus.budgets.json`:
```json
{
  "routing": {
    "policy": "cost-cap,quality-floor",
    "cost_max_per_1m": 5.0,
    "quality_min_score": 70
  }
}
```

Override via CLI: `plutus-route --policy runway`

## Adding custom providers

Add any OpenAI-compatible endpoint to `plutus.budgets.json`:

```json
{
  "providers": {
    "my-provider": {
      "endpoint": "https://api.example.com/v1",
      "api_key_env": "MY_PROVIDER_KEY",
      "budget_usd": 100.0,
      "models": {
        "flagship": "model-name",
        "subtask": "model-name-fast"
      }
    }
  }
}
```

Plutus discovers providers from your Hermes config (or env vars) plus the budgets file. No hardcoded limits.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `PLUTUS_HERMES_CONFIG` | Hermes config path | Provider keys + routing config |
| `PLUTUS_STATE_DB` | Hermes state.db path | Session/spend ledger |
| `PLUTUS_BUDGETS` | `./plutus.budgets.json` | Provider budgets, alerts, routing policy |
| `PLUTUS_PROVIDERS` | `deepseek,anthropic,google,openai` | Which providers to track |
| `PLUTUS_SNAPSHOTS` | `./plutus.snapshots.jsonl` | Burn-rate history |

## Alerts

```json
{
  "alerts": {
    "email": {
      "to": "you@example.com",
      "balance_threshold_usd": 10.0,
      "days_left_threshold": 3
    },
    "discord": {
      "webhook_url": "https://discord.com/api/webhooks/..."
    },
    "ntfy": {
      "topic": "plutus-alerts",
      "server": "https://ntfy.sh"
    }
  }
}
```

Run: `plutus alert` (or `plutus alert --dry-run` to preview).

## Comparisons

| Tool | Scope | Plutus advantage |
|---|---|---|
| **Braintrust** | LLM eval + observability | Plutus focuses on credit/financial management |
| **Helicone** | LLM observability + proxy | Plutus balances routing by credit runway, not just logging |
| **Finout/Datadog** | General cloud cost management | Plutus is LLM-native — knows model pricing, token costs, provider APIs |
| **LangSmith** | LLM tracing + eval | Plutus handles the money — which provider is cheapest, which has runway |

Plutus is the missing piece between LLM ops tools and cloud cost tools: LLM-specific FinOps.

## Automation

Designed to run on a schedule. Typical cron setup:

```bash
# Every hour: refresh dashboard, snapshot, rebalance
*/60 * * * * cd /path/to/plutus && ./plutus-refresh.sh

# Every 3 days: check estimates
0 9 */3 * * cd /path/to/plutus && plutus alert
```

## Files

| File | Purpose |
|---|---|
| `plutus.py` | Monitor (balance, spend, runway, forecast, calibrate, alert) |
| `plutus_route.py` | Balancer (runway-based routing + policy engine + backtest) |
| `plutus-refresh.sh` | Cron driver |
| `plutus.budgets.example.json` | Budget/alerts/routing template |
| `test_plutus.py` | Test suite |

## License

MIT — see [LICENSE](LICENSE).
