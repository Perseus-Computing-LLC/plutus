# ADR: the Postgres path

**Status:** accepted for 1.0 · **Decision:** stay single-backend (SQLite) for
1.0; record the migration shape now so the frozen schema stays portable.

## Context

Plutus stores state in one SQLite file and runs a thread-per-request server with
a connection per request. Writes are serialized with `BEGIN IMMEDIATE`
(`db.immediate()`), which closes the metering/hard-stop races correctly but means
**`/v1/usage` admits one writer at a time**. For a homelab / single-tenant
self-host that is the right trade (zero-dependency, single file, trivial backup).
For a multi-tenant hosted SaaS it is a throughput ceiling.

The 1.0 goal is to **freeze a correct contract**, not to chase scale. Adding a
Postgres backend now would mean freezing a second backend's behavior and a SQL
abstraction layer before we have load evidence — premature.

## Decision

1. **1.0 ships SQLite-only.** No Postgres backend, no ORM, no abstraction layer
   in 1.0.
2. **Keep the schema Postgres-portable** so the future migration is mechanical,
   not a rewrite. The current schema is already close: only `TEXT`/`INTEGER`/
   `REAL` columns, parameterized SQL everywhere, money as integer micros, and the
   running balance computed *in the INSERT* (`add_ledger`) — a pattern that
   ports to Postgres unchanged.
3. **Revisit when there is evidence** of sustained concurrent-write contention on
   a real deployment (not before).

## Known portability items (to address at migration time, not in 1.0)

- **`rowid` cursors.** Pagination (`/api/ledger`, `/api/events`) and the export
  cursor use SQLite's implicit `rowid` (exposed as `_rowid`). Postgres has no
  `rowid`; map this to an explicit `BIGSERIAL`/sequence column or a `(ts, id)`
  compound cursor. The `_rowid` field in `openapi.yaml` is deliberately opaque so
  this can change without a contract break.
- **Write serialization.** `BEGIN IMMEDIATE` becomes a `SERIALIZABLE` transaction
  (or targeted `SELECT … FOR UPDATE` on the org's balance) in Postgres; the
  `db.immediate()` seam already isolates this.
- **`INSERT OR IGNORE` / `INSERT OR REPLACE`** (idempotency claims, the `meta`
  upsert) become `INSERT … ON CONFLICT DO NOTHING / DO UPDATE`.
- **PRAGMAs** (`journal_mode=WAL`, `busy_timeout`, `foreign_keys`) are
  SQLite-only connection setup and simply drop out.

## Migration shape (when triggered)

Introduce a thin `Backend` seam behind `db.connect()` / `db.immediate()` with two
implementations (SQLite, Postgres) sharing the same parameterized SQL where
possible and the same `SCHEMA_VERSION` (see [`schema.md`](schema.md)). A
one-shot exporter copies the append-only ledger and usage events across; because
the ledger is append-only and money is integer micros, the copy is exact and
verifiable by re-summing balances on both sides.
