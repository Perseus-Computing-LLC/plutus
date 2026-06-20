# Plutus Roadmap

> Last updated: 2026-06-19 · Horizon: 12 months

## What Plutus Is

Plutus (Greek god of wealth) is a credit & spend monitor and runway-based model router
for Hermes Agent. It tracks money draining from LLM providers and automatically rebalances
routing toward the provider with the most projected days-left. Part of the Perseus / Mimir /
Mneme / Minions toolset.

Tracks three providers today: **deepseek, anthropic, google**.

## Current State (Jun 2026)

- **Version:** untagged (629 LOC: `plutus.py` 404, `plutus_route.py` 225)
- **Monitors:** live DeepSeek balance API + local `state.db` ledger; budget back-solving via `--calibrate`
- **Balancer:** ranks providers by days-left, rewrites Hermes `config.yaml` routing with backup + round-trip verification + no-op guard
- **Outputs:** CLI table, `--json`, `--html` dashboard, `--snapshot` history
- **Automation:** 2 Hermes cron jobs — credit refresh (60m), balance check-in (3 days)
- **Status:** live and running; internal tool, not yet a tagged public release

---

## 🔴 Critical (this month)

- **Tag v0.1.0** — first public release; freeze the CLI surface.
- **README dashboard screenshot / asciinema** so the repo shows what it does.
- **CI workflow** — lint + `python3 plutus.py --json` smoke test on push.

## 🟡 High Priority (Q3 2026)

- **Low-balance email alert via Himalaya** — fire when DeepSeek < $threshold OR any provider days-left < N. Configurable in `plutus.budgets.json`.
- **Calibration UX** — make the 3-day check-in reply flow (`--calibrate anthropic=NN --calibrate google=NN`) self-documenting in the cron prompt.
- **Add a 4th live-balance provider** — implement a fetcher returning `{"balance_usd":..,"ok":True}` and register in `BALANCE_FETCHERS` + `LEDGER_ALIASES` (OpenAI or OpenRouter first).

## 🟢 Medium Priority (Q4 2026)

- **Multi-provider live balances** — reduce reliance on budget estimates as more providers expose balance APIs.
- **Burn-rate trend lines** — 7/30-day forecast curves on the HTML dashboard, not just point-in-time.
- **Alert channels** — Discord/ntfy in addition to email.

## 🔵 Q1 2027 — Policy Engine

- **Configurable routing policies** beyond pure runway: cost-cap, latency-weighted, quality-floor.
- **Backtest mode** — replay `state.db` history to validate a policy before applying it live.

## Long-term (Q2 2027) — v1.0

- **Generalize beyond 3 providers** to any OpenAI-compatible endpoint with a declared budget.
- **Standalone OSS "LLM FinOps" positioning** — decouple from Hermes-specific assumptions where possible; launch post.

---

## Success Metrics

| Metric | Baseline | 12-mo target |
|---|---|---|
| Release | untagged | v1.0 |
| Live-balance providers | 1 (DeepSeek) | 3+ |
| Alert channels | none | email + 1 push |
| Routing policies | 1 (runway) | 3+ (runway, cost-cap, quality-floor) |
| Public adoption | internal | listed + documented OSS tool |

## Design Principles

1. **Never lose a key.** Config writes back up first and refuse to drop any provider block or API key.
2. **Verify the round-trip.** Re-read config after every write; no-op guard skips unchanged decisions.
3. **Estimate, then self-correct.** Budgets are back-solved from real console balances over time, not guessed once.
4. **Cheap to run.** Plutus must never become a meaningful line item in the spend it monitors.
