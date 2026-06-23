# Plutus — Developer Handoff (continuing toward 1.0)

> For the next Claude Code instance (or human) picking up Plutus development.
> Snapshot: **2026-06-23**, version **0.5.1**.

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
- v0.4.0 self-serve signup funnel · v0.5.0 ingest API (`POST /v1/usage`) + API keys + SDK remote mode · v0.5.1 Cloudflare-UA fix.
- Hermes spend syncs into the dashboard via `examples/hermes_sync.py` on a **host-level cron** (every 15 min) — see `HANDOFF-HERMES-SYNC.md`.
- 79 tests pass (`pytest tests/`). Production monitor untouched.

## The work queue (toward 1.0)
The deep review filed **issues #26–#38**. Sequencing in `ROADMAP-1.0.md`:
- **v0.6 — money & concurrency correctness (1.0 blocker):** #26 webhook idempotency (check-then-act → double-credit), #27 `/v1/usage` not atomic (partial-batch double-count), #30 sqlite concurrency (no `busy_timeout`, non-atomic `balance_after`, quota race), #29 credit from Stripe `amount_total` not metadata, #28 prepaid-credit hard-stop, #38 integer money. **#26/#27/#30 collapse into one "make ingest + webhook atomic + busy_timeout" change — start here.**
- **v0.7 — security hardening (gates public launch):** #31 body-size cap, #32 CSRF + POST logout, #33 signup rate-limit, #34 report escaping, #35 SMTP TLS, #36 OIDC JWKS, #37 polish.
- **v0.8 — Team tier ($149/mo) + LangChain/CrewAI/MCP integrations** (the revenue/distribution lever; can run in parallel).
- **Do not launch publicly before v0.7** — open signup + live money are exposed.

## Critical gotchas (these will bite)
1. **Hermes is NOT a separate box — it's containers on greg.** Reach it via `ssh greg` + `docker exec hermes …`. State.db: `/opt/data/webui/minions-hermes-config/state.db` (in-container).
2. **greg uses HOST `docker compose`, not Dockge** (the dockge container isn't running). Redeploy: `docker compose -f /mnt/cache/appdata/stacks/plutus/compose.yaml pull/up -d --force-recreate plutus`. The `greg-deploy` skill's `docker exec dockge` path does **not** work here.
3. **Cloudflare blocks the default `Python-urllib` User-Agent (error 1010).** Anything POSTing to the public URL with urllib must send a real `User-Agent` (fixed in 0.5.1). Internal service-to-service uses `http://plutus:8420` (same `media-net`), which bypasses CF.
4. **Deployed image is still 0.5.0.** The 0.5.1 UA fix is client-side, so the server needs no redeploy — but **tag `v0.5.1`** to publish the fixed SDK to PyPI (external ingest is broken without it). Greg cron already uses the internal URL, so it's unaffected.
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
curl -fsS https://plutus.perseus.observer/healthz          # {"ok":true,"version":"0.5.x"}
ssh greg 'docker ps --filter name=plutus --format "{{.Names}} {{.Status}}"'
cd <repo> && pytest tests/ -q
```

## Suggested first move
Open the **v0.6 atomic-correctness PR** (#26/#27/#30 together): wrap `/v1/usage` ingest and webhook handling in single transactions, add `PRAGMA busy_timeout`, and make `balance_after`/quota checks atomic. It's the highest-leverage pre-launch work and makes "the money is correct" true.
