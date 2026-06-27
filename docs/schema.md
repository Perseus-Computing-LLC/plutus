# Database schema & forward-compatibility policy

Plutus stores all state in a single SQLite file (`~/.plutus/plutus.db` by
default). This document is the **1.0 forward-compatibility contract** for that
schema — the database half of the frozen contract whose API half is
[`openapi.yaml`](../openapi.yaml).

## Schema version

`plutus_agent.db.SCHEMA_VERSION` is an integer bumped on every schema change and
stamped into the `meta` table key `schema_version` on `init_schema()`. **1.0
ships at schema_version 5.** Read the stored value with `db.get_schema_version(conn)`.

| Version | Change |
|---|---|
| 4 | Money stored as integer micro-dollars (`*_micros`); the `allow_negative_balance` org column. |
| 5 | Adds the `ingest_idempotency` table (per-org `Idempotency-Key` store, #65). |

## The contract (within the 1.0 major line)

1. **Additive only.** Schema changes are limited to **new tables** and **new
   columns that are nullable or have a default**. Existing columns are never
   renamed, retyped, or dropped, and no column gains a `NOT NULL` without a
   default. This keeps an older reader working against a newer database.
2. **Money columns are append-only and integer.** `credit_ledger` is an
   append-only ledger; balances are `SUM(delta_micros)` (see
   [`BILLING.md`](../BILLING.md)). The micro-dollar representation does not change
   in 1.x.
3. **Migrations are idempotent and forward-only.** `init_schema()` is safe to run
   on every startup: `CREATE TABLE IF NOT EXISTS` creates any missing tables, and
   `_migrate_add_columns()` `ALTER`s in any missing columns. There is no
   down-migration — restore from a backup to roll back.
4. **Forward-incompat is refused, not guessed.** Opening a database whose stored
   `schema_version` is **greater** than the running package supports raises
   rather than risk corrupting money data with old code. Upgrade the package.
5. **Breaking changes require a new major.** Anything that violates (1)–(2) — a
   dropped/renamed column, a money-representation change, a semantic change to an
   existing column — ships only in a Plutus 2.0 with an explicit, documented
   migration, and bumps `SCHEMA_VERSION` across the corresponding range.

## Concurrency note

The single-file design serializes writes via `BEGIN IMMEDIATE`
(`db.immediate()`), which is correct but caps `/v1/usage` at one writer at a
time. The horizontal-scale path is documented separately in
[`postgres.md`](postgres.md); it is intentionally **out of scope for 1.0** and is
designed to preserve this contract.
