# Plutus — 12-Month Delivery Plan (Jul 2026 → Jun 2027)

You are running on DeepSeek V4 Pro xhigh. Read this fully, then work.

## Current state
- **Version:** Untagged. 629 LOC across 2 files (plutus.py 404, plutus_route.py 225).
- **Status:** Live internal tool. Not yet a public release.
- **Repo:** https://github.com/Perseus-Computing-LLC/plutus
- **Local:** /opt/data/webui/minions/.minions-data/workspace/plutus/
- **Open issues to fix first:** #1 (zero test coverage — HIGH), #2 (encoding on Windows — LOW), #3 (hardcoded model IDs — MEDIUM)

## What Plutus is
A credit & spend monitor and runway-based model router for Hermes Agent. Named for the Greek god of wealth. Fuses live balance APIs (DeepSeek today) with local ledger spend (Hermes state.db) to project days-left per provider, then automatically rebalances routing toward the provider with the most runway. Tracks three providers: deepseek, anthropic, google.

## Competitive position
Plutus is unique: no other open-source tool combines live balance monitoring, ledger-based spend tracking, budget estimation with self-correcting calibration, and automatic model routing based on runway. LLM routers (Braintrust, Inworld, Helicone) do routing but don't track credit. AI cost observability tools (Finout, Datadog) track spend but don't route. Plutus does both in 629 lines.

## How it works

### Monitor (plutus.py)
- **DeepSeek:** Live balance via `GET /user/balance` → real dollars
- **Anthropic + Google:** Budget-based: `remaining = budget - ledger_spend`. Recalibrated via `--calibrate anthropic=NN.NN` which back-solves the budget from real console balance + current ledger spend.
- **Outputs:** CLI table (default), `--json`, `--html <path>`, `--snapshot` (append burn-rate history)
- **Config:** Reads Hermes `config.yaml` for provider keys, `state.db` for spend ledger, `plutus.budgets.json` for no-API-provider budgets

### Balancer (plutus_route.py)
- Ranks providers by days-left (balance / burn_rate)
- **Primary** = flagship model of highest-runway provider
- **Delegation** (subtasks) = fast model of best non-primary provider
- **Fallbacks** = other two providers, flagship first, fast model second
- Config writes: backup → pre-write key check → write → post-write round-trip verification → no-op guard
- Route log: `plutus.routing.jsonl` (append-only decision audit trail)

### Automation
- 2 Hermes cron jobs: credit refresh (hourly), balance check-in (every 3 days)
- Cron config: `/opt/data/webui/minions-hermes-config/cron/jobs.json`

## 🔴 Critical — this month

### Tag v0.1.0
- Freeze the CLI surface. The `--json`, `--html`, `--snapshot`, `--calibrate` flags are stable enough.
- Add a `--version` flag that prints the tag.

### CI workflow
- Lint + `python3 plutus.py --json` smoke test on push.
- `.github/workflows/test.yml` — simple, one job.

### README dashboard screenshot / asciinema
- The repo needs to show what it does. A terminal recording + HTML dashboard screenshot.

### Tests (plutus#1)
Priority tests, in order:
1. `collect()` returns valid JSON with expected provider keys
2. `calibrate()` correctly back-solves budget from balance + spend
3. `apply()` preserves all top-level config keys and provider blocks after write
4. `apply()` no-op guard correctly skips unchanged routing
5. `deepseek_balance()` handles API errors gracefully (returns `{"ok": false, "error": "..."}`)
6. `ledger_spend()` handles missing state.db (returns empty dict + note, not crash)
7. `plan()` produces correct provider ordering for zero-burn and equal-runway edge cases

## 🟡 Q3 2026 (Jul–Sep) — High Priority

### Low-balance email alert via Himalaya
- Fire when DeepSeek < $threshold OR any provider days-left < N
- Configurable in `plutus.budgets.json`
- The Himalaya CLI (`himalaya send`) is configured for `perseus@perseus.observer`

### Add a 4th live-balance provider
- Implement a fetcher returning `{"balance_usd": ..., "ok": True}`
- Register in `BALANCE_FETCHERS` + `LEDGER_ALIASES`
- OpenAI or OpenRouter first (whichever exposes a balance endpoint)

### Hardcoded model IDs → config (plutus#3)
- Move FLAGSHIP/SUBTASK dicts to `plutus.budgets.json`
- Or: add live model discovery (query `/models` endpoint, validate IDs before writing)
- Current hardcoded values:
  - deepseek: `deepseek-v4-pro` / `deepseek-v4-flash`
  - anthropic: `claude-opus-4-8` / `claude-sonnet-4-5-20250929`
  - google: `gemini-3.1-pro-preview` / `gemini-2.5-flash`

### Calibration UX
- The 3-day check-in cron job's prompt should self-document the `--calibrate anthropic=NN --calibrate google=NN` reply flow.

## 🟢 Q4 2026 (Oct–Dec) — Medium Priority

### Multi-provider live balances
- Reduce reliance on budget estimates as more providers expose balance APIs

### Burn-rate trend lines
- 7/30-day forecast curves on the HTML dashboard
- Data source: `plutus.snapshots.jsonl` (already being appended hourly)

### Alert channels
- Discord (webhook) and/or ntfy in addition to email

## 🔵 Q1 2027 — Policy Engine

### Configurable routing policies
- Beyond pure runway: cost-cap, latency-weighted, quality-floor
- Policy config in `plutus.budgets.json`

### Backtest mode
- Replay `state.db` history to validate a policy before applying it live
- `python3 plutus_route.py --backtest <policy-name>`

## Q2 2027 — v1.0

### Generalize beyond 3 providers
- Any OpenAI-compatible endpoint with a declared budget
- Decouple from Hermes-specific assumptions where possible

### Standalone OSS "LLM FinOps" positioning
- Launch post, comparison page, "awesome-llm-ops" listing
- Decouple from Perseus/Mimir/Minions toolset branding

## Design principles (non-negotiable)
1. **Never lose a key.** Config writes back up first and refuse to drop any provider block or API key.
2. **Verify the round-trip.** Re-read config after every write; no-op guard skips unchanged decisions.
3. **Estimate, then self-correct.** Budgets are back-solved from real console balances over time, not guessed once.
4. **Cheap to run.** Plutus must never become a meaningful line item in the spend it monitors.

## Known pitfalls
1. **Only DeepSeek has a live balance API** — Anthropic/Google are estimate-based and only as accurate as the last calibration.
2. **`open()` without `encoding='utf-8'`** — plutus#2. Fix trivially with `encoding='utf-8'`.
3. **No `gh` CLI** — GitHub ops go through REST API. Token reads from `/opt/data/webui/minions-hermes-config/cache/bws_cache.json`.
4. **Config edits are high-stakes** — the user has zero tolerance for sloppy serialization. The backup + verify + round-trip pattern in `plutus_route.py` exists for this reason.
5. **Azure is DEAD** — standing directive: "drop Azure, all-in AWS." Plutus tracks only deepseek/anthropic/google.

## Your job
Start with the 🔴 critical items. plutus#1 (tests) is the highest-impact — it gates everything else by making changes safe. Write the 7 priority tests, then tag v0.1.0, then set up CI. After that, plutus#3 (move model IDs to config) followed by the 4th provider. The architecture is solid enough to scale to v1.0 as-is — the work is execution, not redesign.
