# Plutus — Roadmap to 1.0

> Last updated: 2026-06-23 · Current: **v0.5.1** (billing engine; PyPI + GHCR + live at plutus.perseus.observer)
> This is the **billing-engine** roadmap. The older `ROADMAP.md` is the long-term monitor/FinOps vision.

## What 1.0 means

Plutus 1.0 is a **production-grade billing layer for AI agents** that a stranger can self-host or sign up for and trust with real money. That bar means four things, in priority order:

1. **Money is correct** — no double-credit, ingest is atomic, prepaid credit is enforceable, amounts reconcile with Stripe.
2. **It's safe to expose publicly** — the self-serve funnel (open signup + ingest API + Stripe) is hardened against abuse, CSRF, DoS, and injection.
3. **The product loop is complete** — signup → API key → meter → see spend → hit a limit → pay, with the tiers that actually sell (incl. a Team tier).
4. **It's documented and stable** — API reference, self-host guide, SDK quickstarts, and a frozen public API + DB schema under semver.

We do **not** call it 1.0 until the money-correctness and public-exposure items below are closed, because open signup + live Stripe are already on.

---

## Milestones

### v0.6 — Money & concurrency correctness  *(1.0 blocker)*
The deep-review findings that can corrupt billing data. Root cause for most: read-modify-write with no atomic transaction under the threaded, connection-per-request server.
- **#27** make `/v1/usage` atomic per request (validate-all → one transaction → commit once)
- **#26** webhook idempotency: insert the dedup row *first*, apply side-effect only if newly inserted
- **#30** `PRAGMA busy_timeout`, atomic `balance_after`, fix the free-tier quota race
- **#29** credit from Stripe `amount_total`, never client metadata
- **#28** prepaid-credit hard-stop policy (stop debiting past zero; opt-in 402)
- **#38** store money as integer micro-dollars (schema migration) — do before freeze
- *Exit:* a concurrency/load test on the ingest + webhook paths proves no double-count, no lost writes, correct balances.

### v0.7 — Security hardening  *(1.0 blocker — gates public launch)*
- **#31** request body-size cap on `/v1/usage` + `/webhook/stripe` (DoS)
- **#32** CSRF tokens on state-changing POSTs; make logout a POST
- **#33** signup rate limiting + per-day org cap (abuse)
- **#34** escape attacker-controlled names in HTML/PDF reports
- **#35** SMTP: TLS-only login, 465 support
- **#36** OIDC JWKS signature verification (defense-in-depth)
- **#37** polish punch-list (error-leak, 404 escaping, hook backup, etc.)
- *Exit:* an external security-review pass on the public surface.

### v0.8 — Product completeness  *(what makes it sell)*
- **Team tier (~$149/mo)** — multi-seat, more workspaces, the missing money tier (drives ramen MRR).
- Per-org credit-enforcement policy (the #28 hard-stop, surfaced in dashboard + API).
- **Usage export** — CSV + webhook out (cost-attribution / "$/task" for customers' own billing).
- **First-class integrations** — LangChain & CrewAI callback handlers, and a Plutus **MCP server** so agents meter themselves.
- Dashboard: date-range selector, per-workspace drill-down, API-key last-used/usage view.

### v0.9 — Hardening, observability & docs
- Structured request logging + a `/metrics` endpoint; per-request ids.
- Full docs site: **API reference**, self-host guide, SDK quickstarts (Python now; TS later), migration notes.
- Backup/restore + schema-migration tooling for self-hosters.
- Soak-test the hosted instance; define SLOs.

### v1.0 — Freeze & launch
- **Freeze the public API + DB schema**; commit to semver.
- Public **status page**; documented upgrade path.
- Cut 1.0, then run the launch (below).

---

## Get it out there  *(parallel GTM track — start now)*

Already done: README reframed, `pip install plutus-agent`, GHCR image, hosted dashboard, Hermes dogfooding live.

**Pre-launch (do alongside v0.6/v0.7):**
- Merge + release the **v0.5.1 Cloudflare-UA fix** (#PR 25) — external SDK ingest is broken through CF without it; hard launch blocker.
- Turn on `PLUTUS_ALLOW_SIGNUP` on the hosted instance once v0.7 abuse controls land.
- Launch assets: a 60-second "meter your agent in 3 lines" asciinema/GIF, dashboard screenshots, polish `/pricing`, a landing section on perseus.observer/plutus.
- A short **"Plutus vs Helicone / Langfuse / OpenMeter"** positioning post — own *billing* (charging end-users), not just observability.

**Launch (after v0.7):**
- Show HN, r/LocalLLaMA, MCP/agent-framework communities; list in LangChain/CrewAI integration directories.
- First 5 design-partner orgs from the warm network; 1–2 reference customers billing their own users through Plutus.

**Don't launch publicly before** the v0.7 items (CSRF, body limits, signup rate-limit) — open signup + live money are exposed today.

---

## Success metrics for 1.0
| Metric | Now | 1.0 target |
|---|---|---|
| Money-correctness bugs (open) | 5 | 0 |
| Public-surface security findings (open) | 7 | 0 |
| Paying orgs | 1 (us) | 10+ |
| First-class integrations | adapters + hook | + LangChain, CrewAI, MCP |
| API stability | unfrozen | frozen, semver |
| Docs | README + 3 docs | full reference + guides |

## Sequencing note
v0.6 and v0.7 are the 1.0 blockers and should land before any public push — they're tracked as issues #26–#38. v0.8 (Team tier + integrations) can proceed in parallel since it's the revenue/distribution lever from the profitability plan. The fastest credible path: **v0.6 money-correctness as one focused sprint (it's mostly one transaction-shaped fix), then v0.7 security, launching as v0.7 ships and tagging 1.0 once v0.8 + docs settle.**
