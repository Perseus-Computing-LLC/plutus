# Changelog

All notable changes to Plutus are documented here.

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
