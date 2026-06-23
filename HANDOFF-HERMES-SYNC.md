# Handoff — wire Hermes spend into the hosted Plutus dashboard

**For:** an agent (Opus 4.8) running on the Hermes box, in this workspace
(`/opt/data/webui/minions/.minions-data/workspace/plutus/`).
**Goal:** every Hermes session's spend shows up live on
`https://plutus.perseus.observer` (we're dogfooding Plutus on our own usage).
**Effort:** ~10 min. One-time deploy + a cron. Read-only against `state.db`.

> ⚠️ **Secret hygiene:** this repo is **public**. Never commit the API key, and
> never paste it into a file under the repo. It comes from the environment only.

---

## What's already done (no action needed)

- Hosted Plutus is **live at `https://plutus.perseus.observer`** (v0.5.0,
  healthy). The ingest API `POST /v1/usage` is up and authenticates by API key.
- The org **Perseus Computing** (`org_b3471ab33602c4c3`, Pro/unlimited) is seeded.
- A write-only ingest **API key has been minted** for that org. The operator
  will provide it to you as `PLUTUS_API_KEY` (see step 1). It can only meter
  usage — it cannot read data, charge, or refund.
- The bridge script ships in this repo: **`examples/hermes_sync.py`**
  (stdlib-only, no `plutus_agent` install needed).

## What you're deploying

`examples/hermes_sync.py` reads **new** rows from Hermes' `sessions` table — the
same `state.db` that `plutus.py` reads — and POSTs them to `/v1/usage`. It tracks
a `sessions.rowid` watermark in a JSON state file and advances it per batch, so
**re-runs never double-count** and a failed run resumes cleanly.

---

## Steps

### 1. Confirm inputs

```bash
cd /opt/data/webui/minions/.minions-data/workspace/plutus
git pull                                   # gets examples/hermes_sync.py + this doc

# Provided by the operator out-of-band — do NOT hard-code or commit:
export PLUTUS_API_KEY='plutus_sk_…'        # the Perseus Computing ingest key
export PLUTUS_REMOTE_URL='https://plutus.perseus.observer'
# Optional (these are the defaults; override only if your paths differ):
# export PLUTUS_STATE_DB='/opt/data/webui/minions-hermes-config/state.db'
# export PLUTUS_WORKSPACE='hermes'
```

If `PLUTUS_API_KEY` isn't set, **stop** and ask the operator for it (or mint one
on greg: `docker exec plutus plutus keys create --name hermes --org "Perseus Computing"`,
which prints the secret once).

Sanity-check the endpoint and the DB:

```bash
curl -fsS "$PLUTUS_REMOTE_URL/healthz"     # expect {"ok": true, "version": "0.5.x", ...}
ls -l "${PLUTUS_STATE_DB:-/opt/data/webui/minions-hermes-config/state.db}"
```

### 2. Dry-run (no writes, watermark untouched)

```bash
python3 examples/hermes_sync.py --dry-run
```

Expect a count of new sessions and a sample of 2–3 events (provider, model,
tokens, `cost_usd`, `workspace:"hermes"`, `source:"hermes"`). If it says
"nothing new", the table is empty or already synced — fine.

### 3. Backfill (first real sync)

```bash
python3 examples/hermes_sync.py
# → "plutus: metered N session(s) → … (watermark rowid=…)"
```

### 4. Verify

- **Idempotency:** run it again immediately — it must print **"nothing new"**
  (proves the watermark works and we won't double-count on cron).
  ```bash
  python3 examples/hermes_sync.py
  cat ~/.plutus/hermes_sync.json          # {"last_rowid": …, "count": …}
  ```
- **It landed:** ask the operator to open `https://plutus.perseus.observer`
  (Google sign-in) and confirm a **`hermes` workspace** with spend now appears.
  (The dashboard read APIs are session-gated, so you can't curl them with the
  ingest key — rely on the script's printed summary + the operator's eyeball.)

### 5. Install the cron (every 15 min)

Mirror the existing `plutus-refresh.sh` cron style. Put the secret in the cron
environment, not in any committed file:

```cron
*/15 * * * * cd /opt/data/webui/minions/.minions-data/workspace/plutus && \
  PLUTUS_REMOTE_URL=https://plutus.perseus.observer PLUTUS_API_KEY=<key> \
  python3 examples/hermes_sync.py >> /var/log/plutus-hermes-sync.log 2>&1
```

Confirm it's registered (`crontab -l`) and watch one cycle in the log.

---

## Gotchas

- **Don't disturb the existing `plutus.py` / `plutus_route.py` crons** — this is
  a *new, additive* job. The monitor keeps running as-is.
- **`state.db` is opened read-only** (`mode=ro`); the bridge never writes to it.
- **Cost:** the bridge sends Hermes' own `actual_cost_usd` (falls back to
  `estimated_cost_usd`), so dashboard dollars match Hermes' ledger.
- **Balance goes negative** on the Perseus Computing org (Pro = unlimited tokens
  but no prepaid credit loaded). Harmless for our own org; `plutus topup` on greg
  if you want it to read clean.
- **Schema drift:** the bridge auto-detects whether `sessions` has `model` /
  `task_type` columns, so it won't break if they're absent.

## Rollback

```bash
crontab -e            # remove the */15 line
rm -f ~/.plutus/hermes_sync.json   # forget the watermark (a later run re-syncs all)
```
No data on the Hermes side is touched; on the Plutus side, over-sent events are
just extra usage rows on our own org.

## Done when

`crontab -l` shows the job, the log shows a clean cycle, a second manual run
says "nothing new", and the operator sees the `hermes` workspace on the
dashboard.
