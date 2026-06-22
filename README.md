# Plutus — the billing layer for AI agents

[![Test](https://github.com/Perseus-Computing-LLC/plutus/actions/workflows/test.yml/badge.svg)](https://github.com/Perseus-Computing-LLC/plutus/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/plutus-agent.svg)](https://pypi.org/project/plutus-agent/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **You wouldn't run a SaaS without billing. Don't run AI agents without Plutus.**

Plutus is **self-hosted, Stripe-integrated usage metering and prepaid-credit billing for LLM / AI-agent spend.** Point your agents at it and you get a real-time dark dashboard of where the money goes — per organization, per workspace, per provider, per task type — with prepaid credits that deplete as calls route through, low-balance and budget-cap alerts, and monthly PDF spend reports.

Everything except Stripe works **fully offline**. State is a single SQLite file. No Plutus cloud. No lock-in.

```bash
pip install plutus-agent
plutus demo            # → dark dashboard with realistic data at http://localhost:8420
```

<p align="center"><em>◆ <a href="https://perseus.observer/plutus/">perseus.observer/plutus</a> · Perseus Computing LLC</em></p>

---

## Start in 30 seconds

```bash
pip install plutus-agent          # PyPI; stdlib + PyYAML, Stripe optional
plutus demo                       # zero-setup tour with a month of sample data
#   → open http://localhost:8420
```

Real setup is three commands:

```bash
plutus init --org "Acme Agents" --tier pro --workspace ci --budget 100
plutus topup --amount 50          # add prepaid credit (Stripe does this in prod)
plutus meter --provider anthropic --model claude-opus-4-8 \
             --task code_review --workspace ci --input 8200 --output 2400
plutus serve                      # your live dashboard at :8420
```

Or in your agent code:

```python
from plutus_agent import Meter

plutus = Meter(org="Acme Agents")
resp = client.messages.create(model="claude-opus-4-8", ...)
plutus.track(provider="anthropic", model="claude-opus-4-8",
             task_type="code_review", workspace="ci",
             input_tokens=resp.usage.input_tokens,
             output_tokens=resp.usage.output_tokens)
print(plutus.balance())           # remaining prepaid credit
```

## What you get

| | |
|---|---|
| **Real-time dashboard** | Dark-themed (`#0c0814`), spend today/7d/30d/MTD, per-workspace budget bars, provider health, cost-per-task ROI, live activity feed. Numbers refresh every 5s. `plutus serve` at `:8420`. |
| **Multi-tenant** | Organizations → workspaces → users. Meter per workspace, per provider, per model, per task type. |
| **Prepaid credits** | An append-only ledger that depletes as calls route through. Balance is always the sum of deltas — auditable, never drifts. |
| **Stripe billing** | Checkout Sessions for credit top-ups + the $20/mo Pro plan, the Customer Portal for self-serve management, and an **idempotent** webhook handler. Test-mode friendly. |
| **Alerts** | Email on low balance or when a workspace nears/exceeds its monthly budget cap. De-duped, offline-safe (dry-run without SMTP). |
| **Monthly reports** | Print-ready spend reports — PDF when `reportlab` is installed, clean HTML otherwise. |
| **Pricing tiers** | Free (10K tracked tokens/mo, 1 workspace), Pro ($20/mo, unlimited tracking, 10 workspaces, credits + alerts + reports), Enterprise (custom, SSO, SLA). |

## Why Plutus — vs. the alternatives

| | Manual console-checking | Spreadsheet | **Nothing** | **Plutus** |
|---|:---:|:---:|:---:|:---:|
| One view across all providers | ❌ (one console each) | 🟡 (you paste it) | ❌ | ✅ |
| Real-time | ❌ | ❌ | ❌ | ✅ (5s refresh) |
| Per-workspace / per-task attribution | ❌ | 🟡 | ❌ | ✅ |
| Prepaid credit that auto-depletes | ❌ | ❌ | ❌ | ✅ |
| Low-balance / budget alerts | ❌ | ❌ | ❌ | ✅ |
| Charge *your* customers (Stripe) | ❌ | ❌ | ❌ | ✅ |
| Cost to run | your time | your time | a surprise invoice | one SQLite file |

If you run agents and you're tracking spend by logging into three billing consoles — or not at all — you already need this.

## The dashboard

`plutus serve` (or `plutus demo`) serves a single dark pane at **`:8420`**:

- **Headline cards** — credit balance (turns coral when low), spend today, month-to-date, tracked-tokens-vs-plan meter.
- **Spend by workspace** — with budget-cap progress bars that go coral past 80%.
- **Providers** — health dot (live/idle/stale), trailing `$/day` burn, last-seen.
- **Cost per task type** — the ROI lens: `$/task` for code review vs chat vs research.
- **Live activity** — the most recent metered calls, estimated vs exact.
- **Billing** — buy prepaid credit, upgrade to Pro, or open the Stripe Customer Portal.

It's framework-free (stdlib `http.server`), CSP-safe, and serves the same on a laptop or a `$5` VPS.

## Stripe (test mode)

Plutus runs fully offline until you give it a key. Then:

```bash
export STRIPE_SECRET_KEY=sk_test_...
export STRIPE_WEBHOOK_SECRET=whsec_...
export STRIPE_PRICE_PRO=price_...          # your $20/mo Price ID
plutus serve
# point Stripe's webhook at  POST http://your-host:8420/webhook/stripe
stripe listen --forward-to localhost:8420/webhook/stripe   # local dev
```

- **Buy credit** → one-time Checkout Session → `checkout.session.completed` tops up the ledger.
- **Upgrade to Pro** → subscription Checkout → subscription webhooks move the org between `pro`/`free`.
- **Manage billing** → Stripe Customer Portal.
- Every webhook is verified and recorded by event id, so a replay never double-credits.

## Deploy with Docker

```bash
docker compose up                  # dashboard at http://localhost:8420 (demo data)
# real use:
docker run -p 8420:8420 -v plutus:/data \
  -e STRIPE_SECRET_KEY=sk_test_... \
  ghcr.io/perseus-computing-llc/plutus serve --host 0.0.0.0
```

State persists in the `/data` volume (`config.yaml` + `plutus.db`).

## Integrations

Thin, dependency-free adapters in [`plutus_agent/integrations`](plutus_agent/integrations) and runnable [`examples/`](examples):

- **Anthropic / OpenAI SDKs** — `track_anthropic(meter, response)` / `track_openai(meter, response)` read `response.usage` for you.
- **Hermes Agent** — push each session as it completes, or back-fill an existing `state.db` ([`examples/hermes_integration.py`](examples/hermes_integration.py)).
- **Claude Code / Codex CLI** — a `Stop`-hook script that meters every turn ([`examples/claude_code_hook.py`](examples/claude_code_hook.py)).

## CLI

```text
plutus init        create ~/.plutus/{config.yaml,plutus.db}
plutus serve       run the dashboard + API at :8420   (--demo for sample data)
plutus demo        serve with realistic sample data (zero setup)
plutus status      orgs, balances, Stripe mode
plutus org         create | list organizations
plutus workspace   create | list workspaces (--budget for a monthly cap)
plutus meter       record a usage event (depletes credit)
plutus topup       add prepaid credit
plutus report      monthly PDF/HTML spend report (--month YYYY-MM)
plutus alerts      deliver pending low-balance / budget alerts
plutus monitor     print live provider runway (bridges to plutus.py)
plutus pricing     show plan tiers
```

## Configuration

`~/.plutus/config.yaml` (created by `plutus init`). Secrets prefer environment variables:

| Env var | Purpose |
|---|---|
| `PLUTUS_HOME` | Plutus home dir (default `~/.plutus`) |
| `PLUTUS_DB` / `PLUTUS_PORT` | Override DB path / dashboard port |
| `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` | Stripe API keys |
| `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_PRO` | Webhook signing secret / Pro Price ID |
| `PLUTUS_SMTP_PASSWORD` | SMTP password for alert email |

Provider price tables (used to *estimate* cost from tokens when an exact cost isn't supplied) are overridable under `pricing.overrides`.

---

## The credit monitor (`plutus.py`)

Plutus started as — and still ships — a **provider credit & spend monitor + runway-based model router** for [Hermes Agent](https://github.com/NousResearch/hermes-agent). These run independently of the billing engine and remain the live FinOps tooling:

- **`plutus.py`** — live DeepSeek balance API + Hermes `state.db` ledger fused per provider; CLI / `--json` / `--html` dashboard; `--calibrate` back-solves budgets for providers without a balance API.
- **`plutus_route.py`** — ranks providers by projected days-left and rewrites Hermes routing (primary / delegation / fallbacks), with backup + round-trip verification + a no-op guard so a config write can never lose a key.

```bash
python3 plutus.py                      # pretty CLI table
python3 plutus.py --calibrate anthropic=74.46
python3 plutus_route.py --dry-run      # preview runway-based routing
```

The billing engine can fold this live runway into its dashboard — set `monitor.enabled` + `monitor.command` in `config.yaml`, or run `plutus monitor`. See the original monitor docs in [`ROADMAP.md`](ROADMAP.md) and [`HANDOFF.md`](HANDOFF.md).

## Layout

```
plutus.py, plutus_route.py     the live credit monitor + router (unchanged)
plutus_agent/                  the monetization engine (this package)
  cli.py  config.py  db.py  pricing.py  metering.py  client.py  bridge.py
  reports.py  alerts.py  demo.py
  billing/stripe_client.py     Checkout, Portal, idempotent webhooks
  server/{app,views,api}.py    stdlib dashboard + JSON API at :8420
  integrations/                Anthropic / OpenAI / Hermes adapters
examples/                      quickstart, Hermes, Claude Code hook
tests/                         engine + server test suites
```

## Development

```bash
pip install -e ".[dev]"        # stripe + reportlab + pytest
python -m unittest discover -s tests -v
python -m unittest test_plutus # the original monitor's suite
```

## License

MIT — see [LICENSE](LICENSE). © Perseus Computing LLC.
