# Plutus — Developer Handoff (continuing toward 1.0)

> For the next Claude Code instance (or human) picking up Plutus development.
> Snapshot: **2026-06-26**, version **0.7.0**.

## One-line status
The **billing engine** (`plutus_agent`) is live — on PyPI (`pip install plutus-agent`), GHCR, and deployed at **https://plutus.perseus.observer**. The self-serve loop (open signup → API key → `POST /v1/usage` → dashboard → Stripe upgrade) works end-to-end, and we're **dogfooding it on our own Hermes spend**. A deep code review is done; the path to 1.0 is in **[ROADMAP-1.0.md](ROADMAP-1.0.md)**.

## Where things live
| | |
|---|---|
| Repo | `Perseus-Computing-LLC/plutus` (public, MIT) — `plutus_agent/` is the engine; `plutus.py`/`plutus_route.py` are the original monitor |
| PyPI | `plutus-agent` |
| Image | `ghcr.io/perseus-computing-llc/plutus:latest` (built on `v*` tag via `.github/workflows/release.yml`) |
| Hosted | https://plutus.perseus.observer — container `plutus` on greg, `media-net`, port 8420, data vol `/data` |
| Hosted DB | `/data/plutus.db` in-container (`PLUTUS_HOME=/data`); host: `/mnt/cache/appdata/plutus/data` |
| Docs | `README.md`, `docs/api.md` (ingest API + SDK remote mode), `docs/auth.md`, `docs/claude-code.md` |

## What's done / live
- v0.4.0 self-serve signup funnel · v0.5.0 ingest API (`POST /v1/usage`) + API keys + SDK remote mode · v0.5.1 Cloudflare-UA fix · v0.6.0 money/concurrency correctness · v0.7.0 security hardening.
- Hermes spend syncs into the dashboard via `examples/hermes_sync.py` on a **host-level cron** (every 15 min) — see `HANDOFF-HERMES-SYNC.md`.
- The test suite passes (`pytest tests/`, stdlib-only/offline). Production monitor untouched.

## The work queue (toward 1.0)
v0.6 (money) and v0.7 (security) **shipped** in 0.6.0/0.7.0. A 2026-06-26 foundation review verified them against `main`: most fixes landed, but several closed issues were only **partial**, plus new hygiene gaps. Current queue — see **v0.7.1 — Foundation hardening** in `ROADMAP-1.0.md`:
- **Carryover (reopened):** #28 prepaid hard-stop (off-by-default + racy), #30 atomic `balance_after` + quota race (only `busy_timeout` landed), #32 CSRF fails open, #33 no DB-backed per-day org cap, #37 500 error-leak + 404 reflected-XSS.
- **New:** #47 `plutus --db` crash (`os` not imported in `cli.py`); #48 Windows/macOS CI + Python-matrix/classifier mismatch + `release.yml` double-publish + stale packaging.
- **v0.8 — Team tier ($149/mo) + LangChain/CrewAI/MCP integrations** (the revenue/distribution lever; after the v0.7.1 carryover).
- **Do not launch publicly before the v0.7.1 carryover lands** — open signup + live money are exposed.

## Critical gotchas (these will bite)
1. **Hermes is NOT a separate box — it's containers on greg.** Reach it via `ssh greg` + `docker exec hermes …`. State.db: `/opt/data/webui/minions-hermes-config/state.db` (in-container).
2. **greg uses HOST `docker compose`, not Dockge** (the dockge container isn't running). Redeploy: `docker compose -f /mnt/cache/appdata/stacks/plutus/compose.yaml pull/up -d --force-recreate plutus`. The `greg-deploy` skill's `docker exec dockge` path does **not** work here.
3. **Cloudflare blocks the default `Python-urllib` User-Agent (error 1010).** Anything POSTing to the public URL with urllib must send a real `User-Agent` (fixed in 0.5.1). Internal service-to-service uses `http://plutus:8420` (same `media-net`), which bypasses CF.
4. **Verify the deployed image version before assuming parity.** Check `GET /healthz` (`{"version": …}`) against the latest tag rather than trusting this doc. Client-side SDK fixes don't require a server redeploy, and the greg cron posts to the internal `http://plutus:8420`, so it's unaffected by CF/SDK changes.
5. **Unraid cron persistence:** host cron lives in `/boot/config/plugins/dynamix/*.cron` + `update_cron` → writes `/etc/cron.d/root`. Plain `crontab -l` reads the wrong spool; verify with `crontab -c /etc/cron.d -l`.
6. **The dogfood ingest API key** lives only in the greg cron / dynamix file (and the operator's hands) — never commit it (public repo). Re-mint anytime: `docker exec plutus plutus keys create --name … --org "Perseus Computing"`.
7. **Stripe is LIVE** on the hosted instance. Be careful with billing-path changes.

## Dev workflow
- **Branch → PR → a human merges.** A safety classifier gates heavy/outward-facing actions — **merging to `main`, tagging/releasing, redeploying greg, and editing `~/.claude/settings.json` all require explicit per-action user authorization.** Don't expect to self-merge; prepare the PR and ask.
- Tests: `pytest tests/` (stdlib `unittest`, fully offline). Keep them green.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Secrets come from env, never config/repo (`config._strip_env_secrets` enforces this on save).

## Verify it's healthy
```bash
curl -fsS https://plutus.perseus.observer/healthz          # {"ok":true,"version":"0.7.x"}
ssh greg 'docker ps --filter name=plutus --format "{{.Names}} {{.Status}}"'
cd <repo> && pytest tests/ -q
```

## Suggested first move
Close the **v0.7.1 carryover** (see `ROADMAP-1.0.md`). Highest-leverage: finish #30/#28 — wrap the ledger read-modify-write (`db.add_ledger` plus the quota/hard-stop checks in `metering.record_usage`) in one `BEGIN IMMEDIATE` transaction so the balance/quota decision and the insert are serialized, and default the prepaid hard-stop on for prepaid orgs. Then the quick wins: #47 (`import os` in `cli.py`) and #32 (fail closed in `_same_origin` when `base_url` is unset).
