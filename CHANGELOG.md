# Changelog

All notable changes to Plutus are documented here.

## [0.5.1] — 2026-06-23

### Fixed
- **Ingest blocked behind Cloudflare.** The SDK's remote `Meter` and the Hermes
  sync bridge sent the default `Python-urllib/x.y` User-Agent, which Cloudflare
  (and similar WAFs) hard-block with **error 1010** — so `POST /v1/usage`
  through the public origin failed for any urllib client. Both now send a real
  `User-Agent` (`plutus-agent/<version>`). Caught while dogfooding Hermes.

## [0.5.0] — 2026-06-23

The **usage ingest API** — closes the self-serve loop so a signed-up org can
feed usage into a hosted instance over HTTP, with no SDK or local DB.

### Added
- **`POST /v1/usage`** — Bearer-authed JSON ingest. Meters one event or a JSON
  array (≤1000) via an API key, returns the metered result(s) + month-to-date
  quota. Past the free cap with `pricing.block_over_free_limit` on, it returns
  **402** with an `upgrade_url`.
- **API keys** — per-org `plutus_sk_…` secrets (only a SHA-256 hash is stored;
  the secret is shown once). New `api_keys` table, `db.create_api_key` /
  `api_key_org` / `list_api_keys` / `revoke_api_key`.
- **Dashboard key management** — an API-keys panel (list, create, revoke) with a
  ready-to-paste `curl` snippet; a one-time "key created" page.
- **`plutus keys create|list|revoke`** CLI for self-hosted/local key management.
- **SDK remote mode** — `Meter(remote="https://…", api_key="plutus_sk_…")` (or env
  `PLUTUS_REMOTE_URL` + `PLUTUS_API_KEY`) sends each `track()` to `/v1/usage`
  instead of a local DB. Auto-detected from env, so the bundled adapters and the
  Claude Code hook report to a hosted instance with no code change. A 402 over
  quota returns a non-recorded result rather than raising (won't break an agent);
  `balance()`/`summary()`/`topup()` stay local-only.

### Changed
- Schema version 3 — adds the `api_keys` table (additive; auto-applied on start).
- `/v1/usage` is a public path (it authenticates by API key, not a session).

## [0.4.0] — 2026-06-23

The **self-serve signup funnel** — Plutus turns into a SaaS strangers can buy
without an operator in the loop.

### Added
- **Open signup** (`auth.allow_signup` / `PLUTUS_ALLOW_SIGNUP`). When on, any
  verified Google account that isn't already known gets its *own* new Free-tier
  org as owner. Off by default; the allow-list still takes precedence so
  inviting a teammate and onboarding a stranger stay distinct. See `docs/auth.md`.
- **Free-tier enforcement** in the metering core:
  - Workspace cap (Free = 1) — events tagged with a new workspace fold into the
    org's first workspace instead of creating another; tracking never breaks.
  - Token quota (Free = 10K/mo) — past the cap, events are flagged
    `over_free_limit` (still recorded, so no billing data is dropped). Optional
    hard stop via `pricing.block_over_free_limit`.
  - `metering.tier_status()` — single source of truth for plan limits vs. usage.
- **In-app upgrade nudge** on the dashboard once an org is near (≥75%) or over
  its quota, wired straight to Stripe Checkout.
- **Public `/pricing` page** comparing Free / Pro / Enterprise — the surface the
  nudges point to (reachable without signing in).

### Changed
- `MeterResult` gains `recorded` and `over_free_limit` fields (additive).
- `db.create_org()` accepts `owner_name`.

## [0.3.0] — 2026-06-22

### Added
- **Google OIDC sign-in** for the dashboard and billing endpoints (stdlib only,
  no auth library). Off by default; enable with `auth.enabled` + a Google OAuth
  client. Server-side, revocable sessions (`sessions` table); access is
  allow-listed (existing org members, plus `auth.allowed_emails` /
  `auth.allowed_domain`); the dashboard and APIs are scoped to the signed-in
  user's orgs (`?org=` for a non-member returns 403). See `docs/auth.md`.
- Public-by-default paths when auth is on: `/healthz`, `/webhook/stripe`,
  `/auth/*` — so health checks and Stripe webhooks are never challenged.

### Changed
- Schema version 2 — adds the `sessions` table (additive; auto-applied on start).

## [0.2.0] — 2026-06-21

The **monetization engine** — Plutus becomes the billing layer for AI agents.

### Added
- **`plutus_agent` package** (PyPI `plutus-agent`, console command `plutus`).
- **Multi-tenant model** — organizations → workspaces → users, in SQLite.
- **Usage metering** per provider / model / task-type, with token→cost
  estimation and exact-cost passthrough.
- **Prepaid credit** — append-only ledger that depletes as calls route through;
  balance is the sum of deltas (robust to out-of-order / back-filled inserts).
- **Dark dashboard** at `:8420` (`plutus serve`) — brand `#0c0814`, real-time
  cards, per-workspace budget bars, provider health, cost-per-task, live feed.
  Framework-free (stdlib `http.server`).
- **`plutus serve --demo` / `plutus demo`** — realistic month of sample data.
- **Stripe billing** — Checkout for prepaid credits + the $20/mo Pro plan,
  Customer Portal, and an idempotent webhook handler. Optional + offline-safe.
- **`plutus stripe-setup`** — creates the Pro price in your Stripe account.
- **`plutus install-claude-hook`** — wires Plutus into Claude Code / Codex as a
  Stop hook so every turn meters automatically.
- **Monthly reports** — PDF (reportlab) or print-ready HTML.
- **Alerts** — SMTP low-balance and budget-cap email, de-duped, offline-safe.
- **Pricing tiers** — Free / Pro / Enterprise.
- **Embeddable client** — `from plutus_agent import Meter`.
- **Integrations** — Anthropic / OpenAI / Hermes adapters; runnable examples.
- **Packaging** — `pyproject.toml`, Dockerfile, docker-compose, GHCR + PyPI
  release workflow, expanded CI.

### Unchanged
- The live credit monitor (`plutus.py`) and runway router (`plutus_route.py`)
  are left byte-for-byte intact. The engine bridges to them via subprocess.

## [0.1.0]
- Provider credit & spend monitor (`plutus.py`) and runway-based model router
  (`plutus_route.py`) for Hermes Agent.
