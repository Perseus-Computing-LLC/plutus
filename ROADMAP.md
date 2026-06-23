# Plutus Roadmap

> **For the active path to a 1.0 release of the billing engine, see [ROADMAP-1.0.md](ROADMAP-1.0.md).**
> This document is the longer-term monitor/FinOps vision.

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

## Year 2 (Jul 2027 – Jun 2028) — v1.1 → v1.4

### v1.1 — Adaptive Routing (Q3 2027)
- Route decisions learn from outcomes: if Claude Opus tasks have 95% success vs 80% for Gemini, weight accordingly.
- Per-task-type routing profiles: code review → Opus, quick fix → Flash, research → Gemini.
- Routing audit log: every decision recorded with rationale for later analysis.
- Integration with Perseus task classification: Perseus says what kind of task; Plutus routes to the best model.

### v1.2 — Multi-Workspace Accounting (Q4 2027)
- Track spend per workspace, per project, per task type.
- Budget allocation: "Project X gets $50/month; Project Y gets $200/month."
- Over-budget alerts with the workspace/project that triggered them.
- Per-workspace HTML dashboards with trend lines.

### v1.3 — Provider Arbitrage (Q1 2028)
- Real-time provider switching based on current pricing + availability.
- "Route this task to whichever provider is cheapest AND healthy right now."
- Health-aware: if a provider is returning 429s, automatically shift traffic.
- Multi-provider fallback chains: try Anthropic → fallback to Google → fallback to DeepSeek.

### v1.4 — Cost Attribution + ROI (Q2 2028)
- Per-task cost attribution: "This code review cost $0.14 in Claude Opus tokens."
- ROI dashboard: cost per completed task, cost per merged PR, cost per resolved issue.
- Trend lines: is your per-task cost going up or down over time?
- Export to billing systems (CSV, JSON, webhook).

---

## Year 3 (Jul 2028 – Jun 2029) — v2.0 → v2.3

### v2.0 — Intelligent Provisioning (Q3 2028)
- Predict future spend based on usage patterns + upcoming tasks from Perseus context.
- Auto-provision: "You have 3 large-code-review tasks queued. Top up Anthropic by $5 to avoid mid-task cutoff."
- Multi-provider budget optimization: allocate total budget across providers for maximum task throughput.
- Budget calendar: projected spend for the next 30 days with confidence intervals.

### v2.1 — Spend Forecasting API (Q4 2028)
- REST API for programmatic spend queries: `GET /forecast?workspace=X&days=30`.
- Webhook alerts: POST to a URL when spend crosses a threshold.
- Integration with Perseus context: `@plutus forecast` directive renders spend forecast inline.
- SDK clients: Python, TypeScript for embedding Plutus in other tools.

### v2.2 — Cost-Aware Model Selection API (Q1 2029)
- REST API: `POST /route` with task description → returns optimal model + provider + estimated cost.
- Integrates with Perseus task classification for model routing recommendations.
- Used by external tools, not just Hermes — any agent framework can call the routing API.
- Response includes rationale: "Routed to Gemini Flash: 90% of Claude Opus quality at 30% of cost."

### v2.3 — Unified Cost + Quality Dashboard (Q2 2029)
- Single pane of glass: spend, task success rate, latency, model availability — per workspace.
- "You spent $127 this month. 82% on Anthropic, 15% on Google, 3% on DeepSeek. Task success rate: 94%."
- Quality-adjusted cost: "Claude Opus costs 3x more but only improves success rate by 2%. Consider routing elsewhere."
- Public status page: provider health, current pricing, recommended routing.

---

## Year 4 (Jul 2029 – Jun 2030) — v3.0 → v3.3

### v3.0 — Multi-Tenant Billing Platform (Q3 2029)
- Organizations with multiple users, multiple workspaces, multiple providers.
- Per-user budgets, per-team budgets, organizational roll-up.
- Invoice generation: monthly spend report as PDF with per-workspace breakdown.
- Stripe integration for prepaid credits: teams load $X/month, Plutus routes until exhausted.

### v3.1 — Organization-Wide FinOps (Q4 2029)
- Multi-cloud provider spend aggregation: AWS Bedrock, Azure OpenAI, GCP Vertex AI.
- Unified dashboard across all LLM spend — not just API providers.
- Anomaly detection: "Your spend on Anthropic spiked 300% in the last hour. Investigation?"
- Budget policies: "Never spend more than $500/day across all providers."

### v3.2 — Compliance + Audit (Q1 2030)
- SOC 2-ready audit logging for all spend decisions.
- "Why was this task routed to Claude Opus at $0.03/1K tokens?" — every decision traceable.
- Data residency controls: spend data stays in-region.
- Enterprise SSO (SAML/OIDC) for the Plutus dashboard.

### v3.3 — Usage-Based Billing for Managed Services (Q2 2030)
- Metered billing for Mimir Cloud + Perseus Cloud: per-entity, per-render, per-synthesis-call.
- Free tier: 10K entities, 100 renders/day, 10 syntheses/day.
- Pro tier: unlimited entities, 1K renders/day, 100 syntheses/day.
- Enterprise: custom limits, SLA, dedicated infrastructure.

---

## Year 5 (Jul 2030 – Jun 2031) — v4.0 → v5.0

### v4.0 — Financial Infrastructure for MCP (Q3 2030)
- Billing API used by other MCP servers, not just Perseus Computing products.
- "Add Plutus billing to your MCP server" — drop-in usage metering + Stripe integration.
- Marketplace: third-party MCP servers listed with Plutus-managed pricing.
- Revenue share: Perseus Computing takes X% of marketplace transactions.

### v4.1 — AI Spend Marketplace (Q4 2030)
- Organizations compare their LLM spend against industry benchmarks.
- "You spend $0.03/task on code reviews. The industry median is $0.05. You're efficient."
- Anonymized, aggregated spend data powers the benchmarks.
- Provider reliability scores: uptime, latency, cost stability — all tracked historically.

### v4.2 — Cost Intelligence (Q1 2031)
- "Should you use Claude Opus or Gemini Flash for this task?" — answered with data, not intuition.
- Per-task-type cost benchmarks across the industry.
- Optimization recommendations: "You could save 40% by routing code-review tasks to Gemini Flash."
- Model retirement planning: "GPT-5 is being deprecated. Here's what it would cost to migrate your workloads."

### v5.0 — The Billing Standard for AI Agents (Q2 2031)
- Plutus is how AI agents pay for themselves.
- Agent framework integrations: LangChain, CrewAI, AutoGen all have `plutus` as a billing backend.
- "My agent spent $0.47 today. It completed 23 tasks. $0.02 per task."
- The PayPal-for-AI-agents positioning: every agent framework needs a billing layer.
- Plutus is the default billing layer for the MCP ecosystem.

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
