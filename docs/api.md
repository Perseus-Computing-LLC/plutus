# Usage ingest API

Send agent/LLM usage to a Plutus instance over HTTP — no SDK, no shared database.
This is how a self-serve org feeds the hosted dashboard.

## 1. Get an API key

From the dashboard: **API keys** panel → name it → **Create key**. The secret
(`plutus_sk_…`) is shown **once** — Plutus stores only its hash, so copy it now.

Self-hosting from the box:

```bash
plutus keys create --name "prod agent"   # prints the secret once
plutus keys list
plutus keys revoke key_xxxx…
```

## 2. Send usage

`POST /v1/usage` with `Authorization: Bearer <key>` and a JSON body:

```bash
curl -X POST https://plutus.perseus.observer/v1/usage \
  -H 'Authorization: Bearer plutus_sk_…' \
  -d '{"provider":"anthropic","model":"claude-opus-4-8",
       "input_tokens":1200,"output_tokens":800,
       "task_type":"code_review","workspace":"prod"}'
```

### Body fields

| Field | Required | Notes |
|---|---|---|
| `provider` | ✅ | e.g. `anthropic`, `openai`, `google`, `deepseek` |
| `model` | | used for price-table lookup when `cost_usd` is omitted |
| `input_tokens` / `output_tokens` | | integers; default 0 |
| `cache_read_tokens` / `reasoning_tokens` | | integers; default 0 |
| `cost_usd` | | exact cost; if omitted, estimated from tokens |
| `task_type` | | defaults to `general` (drives the ROI breakdown) |
| `workspace` | | name/slug/id; auto-created within the tier's workspace cap |
| `source` | | free-form tag, defaults to `api` |

Send a **JSON array** of up to 1000 such objects to batch.

### Response

`200` with the metered result and your month-to-date quota:

```json
{
  "org_id": "org_…",
  "recorded": true,
  "event_id": "evt_…",
  "cost_usd": 0.0156,
  "estimated": true,
  "balance_after": 49.98,
  "over_free_limit": false,
  "tracked_tokens_mtd": 2000,
  "tracked_limit": 10000,
  "tier": "free"
}
```

Batches return `{"results": [...], "recorded": N, "blocked": M}` plus the quota
fields.

### From the Python SDK

The bundled `Meter` can post to `/v1/usage` for you — same call as local mode:

```python
from plutus_agent import Meter

plutus = Meter(remote="https://plutus.perseus.observer", api_key="plutus_sk_…")
plutus.track(provider="anthropic", model="claude-opus-4-8",
             input_tokens=1200, output_tokens=800, task_type="code_review")
```

Set `PLUTUS_REMOTE_URL` + `PLUTUS_API_KEY` instead and remote mode is
auto-detected — the provider adapters (`track_anthropic` / `track_openai`) and
the Claude Code hook then report to your hosted instance with no code change.
Over-quota events come back as `recorded=False` rather than raising, so they
never break an agent. `balance()` / `summary()` / `topup()` are local-only.

### Status codes

| Code | Meaning |
|---|---|
| `200` | recorded (or at least one event in a batch recorded) |
| `400` | malformed JSON, or an event missing `provider` |
| `401` | missing/invalid/revoked API key |
| `402` | free-tier token quota reached **and** `pricing.block_over_free_limit` is on — body carries `upgrade_url` |

By default Plutus never drops billing data: past the free cap, events are still
recorded but flagged `over_free_limit`. Set `pricing.block_over_free_limit: true`
to hard-stop ingestion (and return 402) once the cap is hit.

## Security

- Keys are random `plutus_sk_…` secrets; only a SHA-256 hash is stored, so a
  leaked database never exposes usable keys.
- Revoking a key takes effect immediately.
- `/v1/usage` is reachable without a dashboard session — it authenticates by key
  — so it stays usable even with Google sign-in enabled on the dashboard.
