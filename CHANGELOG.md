# Changelog

All notable changes to Plutus are documented here.

## [Unreleased]

## [1.0.0] — 2026-06-27

**Plutus 1.0 — the billing loop is closed and the contract is frozen.** The
ledger is now an auditable mirror of Stripe (refunds, disputes, and failed
payments reverse it idempotently); every money- and quota-bearing input is
guarded; ingest and auth are hardened; self-serve export and a token-scoped admin
API are in; and the `/v1` OpenAPI spec plus the DB forward-compatibility policy
are published as the frozen contract Perseus and Mimir build against. An internal
security review (documented in `docs/security-review-2026-06-27.md`) cleared the
money/auth/tenant surfaces; an external review remains the gate before any public
launch.

### Fixed
- **Orphaned in-flight Idempotency-Key no longer 409s forever (review F3, #80).**
  If a request crashed between claiming an `Idempotency-Key` and storing its
  response, the row stayed `status=NULL` and every retry got `409` permanently.
  A claimed-but-unanswered row older than a 2-minute grace window is now
  reclaimable (re-processed), while a *completed* claim is never reclaimed
  (replay preserved). Added `db.purge_idempotency()` to bound the table.

### Added
- **OpenAPI 3.1 spec for `/v1/*` + the forward-compatibility contract (#67).**
  [`openapi.yaml`](openapi.yaml) documents the frozen `/v1` surface (usage ingest,
  spend export, admin) that Perseus/Mimir build against. [`docs/schema.md`](docs/schema.md)
  states the database forward-compat policy (additive-only within 1.x; breaking
  changes need a new major), and [`docs/postgres.md`](docs/postgres.md) records the
  ADR keeping the single-file SQLite backend for 1.0 while documenting the
  Postgres migration shape. `db.SCHEMA_VERSION` bumped to 5 (the
  `ingest_idempotency` table); `init_schema` now refuses to open a database
  written by a newer Plutus, and `db.get_schema_version()` reads the stamped
  version.

### Security
- **Negative token counts can no longer rewind the free-tier meter (#80).** The
  `/v1/usage` boundary validated only that token fields coerce to int, so a
  negative `input_tokens` (with a non-negative `cost_usd`, dodging the #61 guard)
  rewound `tracked_tokens_mtd` — bypassing the Free-tier quota — and corrupted
  `SUM(tokens)` aggregates. Negatives are now rejected with a `400` at the
  boundary and a `ValueError` in `record_usage`.
- **CSRF synchronizer token as defense-in-depth (#58).** State-changing
  dashboard POSTs now accept a per-session CSRF token in addition to the existing
  fail-closed Origin/Referer check: a request passes if it is same-origin **or**
  carries a valid token. This lets through legitimate requests whose
  Origin/Referer a privacy proxy stripped (which the origin check rejects), while
  a forgery — which can't know the token — is still blocked. The token is
  `HMAC-SHA256(session_token, "plutus-csrf-v1")`, derivable only by the cookie
  holder and never leaking the cookie; it's embedded as a hidden `_csrf` field in
  every dashboard/pricing form. The origin check remains the first gate.
- **Per-IP self-serve signup throttle (#59).** The existing global hourly limiter
  and DB-backed daily org cap (#33) are both global, so one abuser could drain
  the whole daily budget and lock out legitimate signups. A new per-IP cap
  (`auth.max_signups_per_ip_per_day`, default 3; in-memory 24h ring) is checked
  before the global limiter. The client IP is the socket peer by default, or the
  first `X-Forwarded-For` hop when `auth.trust_forwarded_for` is set (for running
  behind a trusted reverse proxy). Existing members signing in are never
  throttled — only new-org self-serve signups.

### Fixed
- **Dashboard "Sign out" chip used an undefined `--muted` CSS var (#56).** The
  signed-in user chip and its Sign-out button now use `var(--dim)`, so the text
  renders in the intended dim gray instead of falling back to the inherited color.

### Changed
- **Package version is single-sourced (#57).** `pyproject.toml` now declares
  `dynamic = ["version"]` reading from `plutus_agent.__version__`, so the wheel
  metadata and `plutus version` can no longer drift apart.

### Tests
- **High-risk auth/tenant coverage (#66, part 3 — closes #66).** Added tests for
  the previously-untested money/auth paths: the hand-rolled OIDC **RS256 verifier**
  itself (a real pure-Python RSA-signed token verifies; a tampered payload and a
  non-RS256 `alg` are rejected — every other auth test had set
  `allow_unsigned_tokens`, so the signature math was never exercised); the
  `_authz_org` **cross-tenant `PermissionError`** path; and the
  `allow_negative_balance` **exemption end-to-end** over HTTP. (The #60/#61/#62
  coverage landed with those fixes.)

### Added
- **Token-scoped admin API (#66, part 2).** A new `/v1/admin/*` surface lets an
  operator script tenant management instead of using the CLI/dashboard only:
  `GET/POST /v1/admin/orgs` (list / create), `POST /v1/admin/credits`
  (`grant`/`adjust` ledger entries), and `GET/POST /v1/admin/keys` (list /
  mint — the secret is returned once). Gated by a single `admin.token`
  (env `PLUTUS_ADMIN_TOKEN`, masked from saved config, constant-time compared);
  with no token configured the API is disabled and returns `404`.
- **Self-serve spend export + cursor pagination (#66, part 1).** New
  `GET /v1/usage/export.csv` and `export.json` (Bearer-authenticated, org-scoped,
  optional `?since`/`?until` epoch bounds) let a customer pull their own usage
  for their books. List endpoints now paginate with a `?limit&before=<_rowid>`
  cursor: new `GET /api/ledger` and `GET /api/events` return `{items,
  next_before, limit}`, and `GET /api/orgs` accepts `?limit&offset`. The
  underlying `db.ledger_history` / `metering.recent_events` gained a `before`
  cursor.

### Security
- **`/v1/usage` ingest hardening (#65).**
  - **Idempotency-Key.** A retried or duplicated POST used to double-count usage
    and double-debit credit (the inverse of the webhook idempotency from #26).
    The endpoint now accepts an `Idempotency-Key` header, claims it atomically
    with the recording (per-org `ingest_idempotency` table), and replays the
    stored response on a duplicate instead of re-recording.
  - **Per-key rate limit.** A leaked/abusive key could fire unbounded batches; a
    per-key token-bucket limiter (config `ingest.rate_per_min` / `burst`) now
    returns `429` when exceeded.
  - **Monitor-bridge lock-down.** The bridge subprocess now requires the command
    to be an absolute path present in `monitor.allowed_binaries` (fail-closed,
    structured argv, `shell=False`), and when auth is on it only shells out for
    an authenticated request — an unauthenticated dashboard hit no longer
    triggers it.

### Added
- **Estimated costs are flagged `unpriced` when no exact model price exists
  (#64).** Whenever a usage event is metered without an exact `cost_usd` and the
  (provider, model) isn't in the price table, the cost falls back to a
  provider/global default — previously with no signal, so a coarse estimate
  looked authoritative. `MeterResult.unpriced` now carries that signal and it is
  surfaced per-event in the `/v1/usage` response. The price table is expanded to
  current 2026 models (adds `claude-fable-5`, the GPT-5 family, more Gemini, and
  xAI / Mistral / Cohere / Meta providers), carries a dated `PRICE_TABLE_AS_OF`
  stamp shown on the pricing page, and `ModelPrice` can now price reasoning
  tokens separately (defaults to the output rate, so existing estimates are
  unchanged). *Deferred:* persisting `unpriced` onto historical dashboard rows
  (needs a `usage_events` column) and cache-*write* token pricing (needs a new
  event token field) — both noted for a follow-up.

### Fixed
- **Money-correctness cluster (#63) — four independent fixes:**
  - **USD-only is enforced.** The credit ledger stores plain USD micro-dollars
    with no currency dimension, so a non-USD top-up was recorded as the wrong
    number of dollars. A configured `billing.currency` other than `usd` now
    raises a clear `BillingError` instead of silently mis-billing.
  - **`past_due` no longer counts as active Pro.** A subscription in dunning
    used to retain full Pro for the whole retry window; Pro is now kept only
    through `active`/`trialing` (Stripe restores Pro on the next `active`).
  - **Credit checkout amounts are bounded** to a finite $1–$10,000 at the form
    boundary — `inf`/`nan`/a 9-figure typo previously passed straight to Stripe.
  - **Month boundaries are computed in UTC**, matching the UTC-epoch event store,
    so the free-tier quota reset and MTD reports no longer shift by the server's
    UTC offset on a non-UTC host.
- **Batch `/v1/usage` no longer hides prepaid-hard-stop rejections (#62).** The
  multi-event summary reported only the free-tier `blocked` count;
  `over_balance` rejections were absent, so a prepaid org past zero credit could
  get a `200` with events silently dropped. The summary now carries
  `over_balance_blocked`, `free_limit_blocked`, and a `blocked` total covering
  both reasons, and the endpoint returns `402` whenever *nothing* landed —
  including a batch split across both rejection reasons (previously it only 402'd
  when a single reason accounted for the whole batch).

### Added
- **Stripe refunds, disputes, and failed payments now reverse the ledger
  (#60).** The webhook handler previously ignored every reversal event, so a
  refunded or charged-back prepaid top-up left the credit on Plutus's
  append-only ledger forever. New handlers: `charge.refunded` posts a negative
  `refund` entry (converging to the charge's cumulative `amount_refunded`, so
  partial/repeat refunds reverse exactly once); `charge.dispute.created` /
  `charge.dispute.funds_withdrawn` post a negative `adjust` for the disputed
  amount (both events for one dispute converge to a single reversal); and
  `invoice.payment_failed` is recorded as a dunning alert. Top-ups are now keyed
  on the PaymentIntent so a dispute (which carries no customer) maps back to its
  org. Reversals converge to a target amount per Stripe reference on top of the
  existing per-event idempotency, so replays can't double-reverse.

### Security
- **Negative `cost_usd` can no longer mint prepaid credit or bypass the hard-stop
  (#61).** A caller-supplied negative `cost_usd` previously flowed to the ledger
  debit path as `-(-x)` — a *positive* credit delta — and slipped past the prepaid
  hard-stop (a negative cost only raises the projected balance). `record_usage`
  now rejects a negative `cost_usd` with a `ValueError`, and `/v1/usage` returns
  `400` for a negative or non-numeric `cost_usd` before any event is recorded.
  Genuine corrections/credits must go through the explicit adjust/grant/refund
  ledger path, never metering.

### Changed
- **Prepaid credit hard-stop is now ON by default (#28).** `pricing
  .block_over_balance` defaults to `true`, so a prepaid org can no longer debit
  unbounded amounts past a zero balance — `/v1/usage` returns `402` with
  `over_balance` once a charge would go negative. It only affects orgs that have
  actually held credit; pure free-tier tracking is never blocked. Trusted /
  internal orgs can opt into track-only mode with a new per-org
  `allow_negative_balance` flag, toggled via `plutus org allow-negative <org>` /
  `plutus org enforce-balance <org>` (idempotent column migration on existing
  databases).

### Fixed — 1.0 punch-list (#37)
- **`org create` / `workspace create` with no NAME** now exit with a usage
  message instead of crashing in `slugify(None)`.
- **500 responses no longer leak `str(exception)`** — both the GET error page and
  the POST JSON return a generic message plus a short reference id; the full
  exception is logged server-side under that id.
- **Reflected-XSS surface closed** — the 404 handler now HTML-escapes the
  request path before rendering it.
- **Ambiguous-org guard** — state-changing POSTs (billing, API keys) require an
  explicit `org` when the signed-in user belongs to more than one, instead of
  silently acting on the earliest org. Dashboard GETs stay lenient.
- **`api_key_org` throttles `last_used_at`** to at most once per 60s per key,
  removing per-ingest WAL thrash / write contention with the metering txn.
- **`install-claude-hook` backup** copies the pristine original bytes once and no
  longer clobbers that backup (or re-serializes away comments) on re-runs.
- **PyYAML-free config reader** now reads back the block-style lists PyYAML
  writes, so a config saved with PyYAML and re-read without it no longer silently
  resets to defaults.

### Security
- **DB-backed per-day signup cap (#33)** — self-serve org creation now has a
  hard ceiling per rolling 24h (`auth.max_new_orgs_per_day`, default 50),
  counted from the `organizations` table so it survives process restarts —
  unlike the existing in-memory hourly limiter, which it complements. Set to
  `0` to disable.
- **OIDC unsigned-token bypass removed** — signature verification was skipped for
  any id_token whose header segment literally equalled `"hdr"` (a test shim) on
  the production path. It is now gated behind an explicit, default-off
  `auth.allow_unsigned_tokens` flag used only by the test suite. (#37)

## [0.7.0] — 2026-06-24

Security hardening — the second of the two 1.0 launch-gate milestones. Closes
the public-surface findings so open signup + live money can be exposed safely.
(The roadmap's v0.7 exit also calls for an external security-review pass before
public launch; that human gate is separate and not flipped here.)

### Security
- **Request body-size cap (#31)** — `/v1/usage` and `/webhook/stripe` reject
  bodies over 1 MiB with `413`, closing a trivial memory-exhaustion DoS.
- **CSRF protection (#32)** — cookie-authenticated state-changing POSTs are
  same-origin checked (Origin/Referer vs `auth.base_url`); logout is now a POST
  (`GET /auth/logout` returns `405`).
- **Signup abuse controls (#33)** — self-serve signup is rate-limited
  (5/hour, in-memory global). *(Correction, 2026-06-26: the per-day org cap is
  not yet implemented; tracked in the reopened #33.)*
- **Report XSS escaping (#34)** — attacker-controlled names (org, keys, periods)
  are HTML-escaped in the dashboard and HTML/PDF reports.
- **SMTP TLS (#35)** — implicit TLS on port 465 (`SMTP_SSL`) and STARTTLS on
  other ports before any `LOGIN`; no credentials sent in the clear.
- **OIDC JWKS verification (#36)** — Google ID tokens are verified against the
  published JWKS RSA signature (cached 1h) in addition to `aud`/`iss`/`exp`/
  `nonce` claims.

### Polish (#37)
- Strict integer parsing on token fields, `--db` flag wiring, config-file
  backups on write, `email_verified` enforcement, and a YAML-load fallback.

## [0.6.0] — 2026-06-24

Money & concurrency correctness — the first 1.0 launch-gate milestone. Root
cause for most findings: read-modify-write with no atomic transaction under the
threaded, connection-per-request server.

### Fixed
- **Atomic `/v1/usage` (#27)** — validate all events, record them in a single
  transaction, commit once; no partial batches, no double-count.
- **Webhook idempotency (#26)** — insert the dedup row first and apply the
  side-effect only if newly inserted, so retried Stripe events can't double-credit.
- **Concurrency hardening (#30)** — `PRAGMA busy_timeout` + WAL. *(Correction,
  2026-06-26: the atomic `balance_after` and free-tier quota-race fixes were not
  fully completed — the ledger read-modify-write is still non-atomic under
  concurrency; tracked in the reopened #30.)*
- **Trustworthy credit (#29)** — credit prepaid balance from Stripe's
  `amount_total`, never client-supplied metadata.
- **Prepaid hard-stop (#28)** — stop debiting past zero; opt-in `402` when a
  prepaid org is exhausted.
- **Integer micro-dollars (#38)** — all money stored as integer micro-dollars
  (schema migration) to eliminate float drift before the 1.0 schema freeze.

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
