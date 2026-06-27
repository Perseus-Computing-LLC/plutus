# Internal security review — 2026-06-27 (pre-1.0)

A read-only audit of the money, auth, tenant-isolation, ingest, and injection
surfaces ahead of the 1.0 freeze (#67). Every finding was verified against source.
This is the **internal** pre-1.0 pass; an **external** review remains the standing
gate before any public launch.

## Findings & disposition

| ID | Severity | Area | Status |
|----|----------|------|--------|
| F1 | HIGH | Negative token counts via `/v1/usage` rewound `tracked_tokens_mtd` (free-tier bypass) and corrupted `SUM(tokens)` aggregates | **Fixed** — #80 (boundary `400` + `record_usage` guard) |
| F3 | LOW | Orphaned in-flight `Idempotency-Key` `409`'d that key forever (no TTL/reclaim) | **Fixed** — auto-reclaim after a 120s grace + `purge_idempotency()` |
| F2 | MEDIUM | In-memory signup throttles reset on restart / don't span workers | **Accepted for 1.0** — the DB-backed `max_new_orgs_per_day` hard cap bounds the blast radius and survives restarts; `allow_signup` is off by default. Revisit with a DB-backed per-IP counter if abuse appears. |
| F4 | LOW | Webhook claim-then-rollback ordering could double-apply a credit on a narrow path | **Accepted for 1.0** — not exploitable in the current handlers (nothing raises after `add_ledger`); refunds/disputes are additionally converge-to-target. Tighten by making the event-claim + ledger write one transaction when convenient. |

## Verified sound (no action needed)

The high-risk surfaces are in genuinely good shape and reflect a real hardening
history:

- **RS256 verifier** (`auth._verify_rs256_signature`) — enforces `alg==RS256`
  (no `none`/HS256 confusion), strict EMSA-PKCS1-v1_5 structure with a full
  `digestinfo+hash` suffix compare and ≥8-byte padding run, keys from Google's
  JWKS over TLS; the test-only bypass is gated behind a default-off flag.
- **Negative `cost_usd`** — guarded at the boundary and authoritatively in
  `record_usage` (#61).
- **Prepaid hard-stop / balance races** — `BEGIN IMMEDIATE` serializes the
  read+insert; balance computed in-SQL as integer micros (#28/#30).
- **SQL injection** — all queries parameterized; the one f-string column name is
  a fixed dict key, not user input.
- **XSS** — all user-controlled data routed through `html.escape` (`_e`).
- **SSRF / command exec** — the monitor bridge is `shell=False`, absolute-path
  allow-listed, fail-closed, auth-gated (#65).
- **Secrets** — env secrets stripped before save; API-key secrets sha256-only at
  rest; rate-limit buckets on a token hash; 500s leak only a ref id.
- **Tenant isolation** — `_authz_org` raises `PermissionError` cross-tenant;
  admin API constant-time token; webhook→org mapping verified.
- **CSRF** — same-origin (fail-closed) + per-session synchronizer token (#32/#58).
- **Currency / `past_due` / amount bounds / body & batch limits** — all handled
  (#63/#65).

## Bottom line

The one finding held for the release (F1) is fixed; the cheap F3 is fixed; F2/F4
are documented accepted risks for 1.0. The internal pass is clear to freeze.
External review is still required before public launch.
