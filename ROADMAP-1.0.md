# Plutus — Roadmap to 1.0

> Last updated: 2026-06-27 · Current: **1.0.0 (code-frozen; tag/publish pending)**
> This is the **billing-engine** roadmap. The older `ROADMAP.md` is the long-term monitor/FinOps vision.

> **2026-06-27 status:** the 1.0 blocker queue (#60–#66) plus the low-sev
> follow-ups (#56–#59) and the pre-1.0 security-review fixes (#80) are all merged
> to `main`; the `/v1` OpenAPI spec + DB forward-compat policy are published; the
> version is bumped to 1.0.0. The authoritative changelog is the `[1.0.0]` section
> of [`CHANGELOG.md`](CHANGELOG.md) and the roadmap issue
> [#67](https://github.com/Perseus-Computing-LLC/plutus/issues/67). **Remaining
> gates (human/outward):** push the `v1.0.0` tag (publishes to PyPI + GHCR) and an
> external security-review pass before public launch. The milestone framing below
> predates the issue-numbered queue and is kept for historical context.

## What 1.0 means

Plutus 1.0 is a **production-grade billing layer for AI agents** that a stranger can self-host or sign up for and trust with real money. That bar means four things, in priority order:

1. **Money is correct** — no double-credit, ingest is atomic, prepaid credit is enforceable, amounts reconcile with Stripe.
2. **It's safe to expose publicly** — the self-serve funnel (open signup + ingest API + Stripe) is hardened against abuse, CSRF, DoS, and injection.
3. **The product loop is complete** — signup → API key → meter → see spend → hit a limit → pay, with the tiers that actually sell (incl. a Team tier).
4. **It's documented and stable** — API reference, self-host guide, SDK quickstarts, and a frozen public API + DB schema under semver.

We do **not** call it 1.0 until the money-correctness and public-exposure items below are closed, because open signup + live Stripe are already on.

---

## Milestones

### v0.6 — Money & concurrency correctness  *(shipped v0.6.0, 2026-06-24 — partial; see v0.7.1 carryover)*
The deep-review findings that can corrupt billing data. Root cause for most: read-modify-write with no atomic transaction under the threaded, connection-per-request server.
- **#27** make `/v1/usage` atomic per request (validate-all → one transaction → commit once)
- **#26** webhook idempotency: insert the dedup row *first*, apply side-effect only if newly inserted
- **#30** `PRAGMA busy_timeout`, atomic `balance_after`, fix the free-tier quota race
- **#29** credit from Stripe `amount_total`, never client metadata
- **#28** prepaid-credit hard-stop policy (stop debiting past zero; opt-in 402)
- **#38** store money as integer micro-dollars (schema migration) — do before freeze
- *Status (verified 2026-06-26):* ✅ #26, #27, #29, #38 landed. ⚠️ #30 partial (only `busy_timeout`/WAL landed; `balance_after` still non-atomic + quota race remains) and #28 partial (hard-stop is off-by-default and racy) — both carried to **v0.7.1**.
- *Exit:* a concurrency/load test on the ingest + webhook paths proves no double-count, no lost writes, correct balances.

### v0.7 — Security hardening  *(shipped v0.7.0, 2026-06-24 — partial; see v0.7.1 carryover)*
- **#31** request body-size cap on `/v1/usage` + `/webhook/stripe` (DoS)
- **#32** CSRF tokens on state-changing POSTs; make logout a POST
- **#33** signup rate limiting + per-day org cap (abuse)
- **#34** escape attacker-controlled names in HTML/PDF reports
- **#35** SMTP: TLS-only login, 465 support
- **#36** OIDC JWKS signature verification (defense-in-depth)
- **#37** polish punch-list (error-leak, 404 escaping, hook backup, etc.)
- *Status (verified 2026-06-26):* ✅ #31, #34, #35, #36 landed. ⚠️ #32 (CSRF fails open when `base_url` unset / auth off), #33 (no DB-backed per-day org cap; only an in-memory hourly limiter), #37 (500 error-leak + 404 reflected-XSS) partial — carried to **v0.7.1**.
- *Exit:* an external security-review pass on the public surface.

### v0.7.1 — Foundation hardening  *(carryover + hygiene; precondition to v0.8)*
A 2026-06-26 foundation review verified v0.6/v0.7 against `main`. Most fixes landed; the items below were closed but only partially resolved, plus new hygiene gaps. Close these before the v0.8 feature work — 1.0 is the convergence gate for Perseus/Perseus Vault, so the contract must be honest and stable first.
- **#28** prepaid hard-stop: default-on for prepaid orgs + enforce *inside* the debit transaction (currently off-by-default and racy).
- **#30** atomic `balance_after` + free-tier quota race: wrap the ledger read-modify-write in `BEGIN IMMEDIATE` (or one conditional `INSERT…SELECT`).
- **#32** CSRF: fail closed in `_same_origin` when `base_url` is unset, and/or add a per-session token.
- **#33** DB-backed per-day org-creation cap alongside the hourly limiter.
- **#37** stop leaking `str(e)` to clients on 500s; escape the 404 `path`; gate the OIDC `"hdr"` test-bypass behind an explicit flag.
- **#47** `plutus --db` crash (`os` not imported in `cli.py`).
- **#48** add `windows-latest`/`macos-latest` CI; align the Python matrix with the classifiers (3.10/3.13); fix the `release.yml` double-publish trigger; drop stale `assets/` packaging.
- *Exit:* every reopened/new issue above closed; a concurrency test covers the #28/#30 transaction; CI green on Linux + Windows across the advertised Python versions.

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
| Money-correctness bugs (open) | 2 (#28, #30) | 0 |
| Public-surface security findings (open) | 3 (#32, #33, #37) | 0 |
| Paying orgs | 1 (us) | 10+ |
| First-class integrations | adapters + hook | + LangChain, CrewAI, MCP |
| API stability | unfrozen | frozen, semver |
| Docs | README + 3 docs | full reference + guides |

## Sequencing note
v0.6 and v0.7 **shipped** (0.6.0 / 0.7.0), but the 2026-06-26 review surfaced partial fixes + hygiene gaps now collected in **v0.7.1 — Foundation hardening**. Close v0.7.1 before any public push and before wiring Plutus into Perseus/Perseus Vault (the convergence-gated-on-1.0 decision). v0.8 (Team tier + integrations) is the revenue/distribution lever and can proceed in parallel, but the 1.0 tag waits on v0.7.1 + an external security-review pass + a frozen API/schema.
