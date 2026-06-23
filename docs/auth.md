# Authentication — Google sign-in

By default the Plutus dashboard is **open** — fine for `localhost` or when it
sits behind a trusted proxy. To require login, Plutus ships **Google OIDC**
sign-in built on the standard library (no auth framework, no extra dependency).

When enabled:

- Every page and JSON API requires a valid session **except** `/healthz`,
  `/webhook/stripe`, and `/auth/*` (so health checks and Stripe webhooks are
  never challenged).
- Sessions are **server-side and revocable** (a `sessions` row keyed by a random
  token in an `HttpOnly; Secure; SameSite=Lax` cookie).
- Who may sign in is **allow-listed**: anyone already a member of an org, plus
  anyone matching `allowed_emails` or `allowed_domain`.
- The dashboard and APIs are **scoped to the signed-in user's orgs**. Requesting
  an org you don't belong to (`?org=…`) returns `403`.

## 1. Create a Google OAuth client

Google Cloud Console → **APIs & Services → Credentials → Create credentials →
OAuth client ID**:

- Application type: **Web application**
- **Authorized redirect URI:** `https://YOUR-HOST/auth/callback`
  (e.g. `https://plutus.perseus.observer/auth/callback`)

Copy the **Client ID** and **Client secret**.

## 2. Configure Plutus

Prefer environment variables (the client secret is never written to
`config.yaml` — it's stripped on save, like the Stripe key):

| Env var | Meaning |
|---|---|
| `PLUTUS_AUTH_ENABLED` | `1`/`true` to turn sign-in on |
| `PLUTUS_GOOGLE_CLIENT_ID` | OAuth client ID |
| `PLUTUS_GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `PLUTUS_BASE_URL` | Public origin, e.g. `https://plutus.perseus.observer` (used to build the redirect URI) |
| `PLUTUS_ALLOWED_EMAILS` | Comma-separated extra emails allowed to sign in |
| `PLUTUS_ALLOWED_DOMAIN` | Any address at this domain may sign in, e.g. `perseus.observer` |
| `PLUTUS_ALLOW_SIGNUP` | `1`/`true` for **open signup** — any verified Google account gets its own new Free-tier org |

Equivalent `config.yaml`:

```yaml
auth:
  enabled: true
  google_client_id: "…apps.googleusercontent.com"
  google_client_secret: ""        # leave empty here; supply via env
  base_url: "https://plutus.perseus.observer"
  allowed_emails: []
  allowed_domain: ""
  provision_org_id: ""            # newly-allowed emails join this org (or the sole org)
  allow_signup: false             # true = open self-serve signup (see §3)
  session_ttl_hours: 168
```

> **Safety valve:** if `auth.enabled` is `true` but the client ID/secret are
> missing, Plutus treats auth as **off** rather than locking everyone out of a
> misconfigured server. The startup banner shows the effective auth mode.

## 3. Members & provisioning

Sign-in resolves an email in this order:

1. **Existing member** → signs in as themselves (the org owner created by
   `plutus init` already counts).
2. **Allow-listed** (via `allowed_emails`/`allowed_domain`) → provisioned as a
   `member` of `provision_org_id`, or of the only org if there is exactly one.
   This is how you invite teammates into an existing org.
3. **Open signup** (`allow_signup: true`) → any other verified Google account
   gets its **own** brand-new Free-tier org, as `owner`. This is the self-serve
   SaaS path: strangers can sign up without an operator in the loop.
4. Otherwise → denied.

The allow-list takes precedence over open signup, so "invite a teammate into my
org" (step 2) and "let anyone sign up" (step 3) stay distinct. Keep
`allow_signup` **off** for a private/single-tenant instance; turn it on for the
public hosted dashboard.

### Free-tier limits & the upgrade funnel

New self-serve orgs start on **Free** (10K tracked tokens/month, 1 workspace).
Limits are enforced in the metering core, not the UI:

- **Workspaces** — at the cap, a usage event tagged with a new workspace folds
  into the org's first workspace rather than creating another. Tracking never
  breaks.
- **Tracked tokens** — past the monthly cap, events are still recorded but
  flagged `over_free_limit` (no billing data is ever silently dropped). Set
  `pricing.block_over_free_limit: true` to hard-stop recording past the cap.

The dashboard shows a usage meter and, once an org is near (≥75%) or over its
quota, an **Upgrade to Pro** nudge that links to `/pricing` and Stripe Checkout.
`/pricing` is public so prospects can compare plans before signing in.

## 4. Self-hosting note

Cloudflare Access (or any external SSO proxy) can gate Plutus too, but it
doesn't scale to *your* end users — they'd each need access to your Zero Trust
org. App-native OIDC is what lets customers run Plutus and sign in with their
own Google accounts. Use a proxy as an interim guard; rely on this for the real
thing.

## Hardening backlog

- The `id_token` is trusted because it's fetched directly from Google's token
  endpoint over TLS; add JWKS/RSA signature verification if you ever accept
  tokens from a less trusted path.
- Add more OIDC providers (the flow is provider-agnostic; only the endpoints and
  client creds differ).
