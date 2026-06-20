# Plutus — Session Handoff

**Repo:** https://github.com/Perseus-Computing-LLC/plutus (public, MIT)
**Local:** `/opt/data/webui/minions/.minions-data/workspace/plutus/`
**Status:** Live and running. Monitoring + auto-balancing active.

---

## What Plutus is

Named for the Greek god of wealth. Plutus is a credit & spend monitor for Hermes
Agent that watches the money draining out of every LLM provider you use — and
automatically rebalances model routing toward whichever provider has the most
runway. It sits alongside Perseus / Mimir / Mneme / Minions in the toolset.

It tracks exactly three providers (the only ones the user cares about):
**deepseek, anthropic, google**.

---

## What it does (today, working)

### 1. Monitors — `plutus.py`
Fuses two data sources per provider:
- **Live balance API** where one exists. DeepSeek: `GET /user/balance` → real
  dollars. (Anthropic/Google have no usable balance API with the current keys.)
- **Local spend ledger** for the rest, read from Hermes `state.db` → `sessions`
  table (`billing_provider`, `estimated_cost_usd`/`actual_cost_usd`, token
  counts). Gives spend today/7d/30d/all, burn rate, and projected days-left.

For no-balance providers, the user supplies a **budget** and Plutus computes
`remaining = budget - ledger_spend`. `--calibrate` back-solves the budget from
the real console balance so estimates self-correct over time.

Outputs: CLI table (default), `--json`, `--html <path>` dashboard, `--snapshot`
(append burn-rate history).

### 2. Balances — `plutus_route.py`
Ranks the three providers by projected days-left, then rewrites Hermes routing:
- **Primary** = flagship model of the highest-runway provider
- **Delegation** (subtasks) = fast model of the best non-primary provider
- **Fallbacks** = the other two providers, flagship first, fast model second

Every config write: backs up `config.yaml` first, **refuses to write if any
provider block or API key would be lost**, re-verifies the round-trip after
writing, and a no-op guard skips writes when the decision hasn't changed.
All model IDs are verified live against each provider's `/models` endpoint
before promotion (this caught `gemini-3-pro-preview` being 404/retired —
`gemini-3.1-pro-preview` is the live flagship).

### Current routing (as of 2026-06-19)
- **Primary:** google / gemini-3.1-pro-preview  (~4000+ days runway, barely used)
- **Delegation:** anthropic / claude-sonnet-4-5-20250929
- **Fallbacks:** anthropic/claude-opus-4-8 → deepseek/deepseek-v4-pro →
  anthropic/claude-sonnet-4-5 → deepseek/deepseek-v4-flash
- DeepSeek (live $57.76, ~6 days runway) pushed out of the hot path so it stops
  bleeding. It was the bottleneck.

### Current balances
- deepseek:  ~$57.76 (LIVE from API), ~6 days
- anthropic: ~$52.63 (estimate: $74.46 budget − spend), ~16 days
- google:    ~$93.44 (estimate: $93.59 budget − spend), effectively unlimited

---

## Automation (Hermes cron jobs)

| Job | ID | Schedule | What |
|---|---|---|---|
| Plutus credit refresh | `p1u7u5cred01` | every 60m | `plutus-refresh.sh`: regen dashboard, snapshot history, **re-run the balancer** |
| Plutus balance check-in | `p1u7u5cal1b8` | every 3 days | Asks the user for the real Anthropic/Google balances, then recalibrates |

Cron config: `/opt/data/webui/minions-hermes-config/cron/jobs.json`
Refresh script: `/opt/data/webui/minions-hermes-config/cron/plutus-refresh.sh`

---

## What it WILL do (next session pickup)

The system is autonomous now. Things a future session may be asked to do:

1. **Recalibrate** when the user replies to a 3-day check-in with real balances:
   `python3 plutus.py --calibrate anthropic=NN.NN --calibrate google=NN.NN`
   (back-solves budget; dashboard auto-refreshes).
2. **Watch Google's runway fall.** Google now absorbs primary traffic, so its
   ~4000-day figure will drop fast. The hourly balancer will re-rank and reroute
   automatically — verify it's behaving as Google approaches Anthropic's runway.
3. **Optional adds the user floated:** CI workflow (lint + `plutus.py --json`
   smoke test), dashboard screenshot/asciinema in README, low-balance email
   alert via Himalaya (DeepSeek < threshold or days-left < N).
4. **Add a live-balance provider:** write a fetcher returning
   `{"balance_usd":..,"ok":True}`, register in `BALANCE_FETCHERS` + `LEDGER_ALIASES`.

---

## Files

| File | Purpose | Committed? |
|---|---|---|
| `plutus.py` | Monitor + calibrate | yes |
| `plutus_route.py` | Balancing arm | yes |
| `plutus-refresh.sh` | Cron driver | yes |
| `plutus.budgets.example.json` | Budget template | yes |
| `README.md`, `LICENSE`, `.gitignore` | Package | yes |
| `plutus.budgets.json` | **Real** budgets ($74.46/$93.59) | NO — gitignored |
| `plutus.html`, `*.jsonl`, `*.log`, `*.bak` | Runtime artifacts | NO — gitignored |

---

## Critical context / gotchas

- **DeepSeek is the only LIVE balance.** Anthropic/Google are estimate-based and
  only as accurate as the last calibration. The 3-day check-in keeps them honest.
- **No `gh` CLI here.** GitHub ops go through REST API + git over HTTPS, with the
  token read at runtime in `execute_code()` from
  `/opt/data/webui/minions-hermes-config/cache/bws_cache.json` (`secrets.GITHUB_TOKEN`).
  Credential redaction mangles tokens in any tool ARGUMENT — never inline them.
- **Config edits are high-stakes.** User has zero tolerance for sloppy config
  serialization. plutus_route.py's backup + verify + round-trip checks exist for
  this reason. A rollback backup is at
  `config.yaml.plutus-bak-<timestamp>` if routing ever needs reverting.
- **Azure is DEAD.** Standing directive: "drop Azure, all-in AWS." Any recalled
  memory walking through Azure portal / pasting an Azure key is STALE history,
  archived 2026-06-19. Do not act on it. Plutus tracks only deepseek/anthropic/google.
- **New sessions** pick up the Google-primary routing immediately; the session
  that applied it keeps its old model until restart.
