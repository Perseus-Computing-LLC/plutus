# Billing setup — get Plutus accepting money in ~10 minutes

Plutus runs fully offline until you add a Stripe key. This is the end-to-end
flow to take a real (test-mode) payment and watch prepaid credit top up.

## 0. Prerequisites
```bash
pip install "plutus-agent[stripe]"
plutus init --org "Your Co" --tier pro
```
Grab your **test-mode** keys from the Stripe dashboard (Developers → API keys).
Always start in test mode (`sk_test_…`).

## 1. Point Plutus at Stripe
```bash
export STRIPE_SECRET_KEY=sk_test_xxx
export STRIPE_PUBLISHABLE_KEY=pk_test_xxx
export STRIPE_WEBHOOK_SECRET=whsec_xxx        # from step 3
```
Or put them in `~/.plutus/config.yaml` under `billing:` (env wins).

## 2. Create the Pro plan
```bash
plutus stripe-setup
```
This creates a **Plutus Pro** product + a `$20/mo` recurring price (idempotent
via the `plutus_pro_monthly` lookup key) and writes the price id into your
config. Credit top-ups are priced dynamically per checkout, so nothing else to
create.

## 3. Run the server + forward webhooks
```bash
plutus serve            # dashboard at http://localhost:8420
# in another terminal (Stripe CLI):
stripe listen --forward-to localhost:8420/webhook/stripe
```
`stripe listen` prints a `whsec_…` — that's your `STRIPE_WEBHOOK_SECRET` for
step 1. Restart `plutus serve` after setting it.

## 4. Take a payment
On the dashboard's **Billing** panel: enter an amount → **Top up →** → pay with
Stripe's test card `4242 4242 4242 4242` (any future expiry / CVC). Or fire a
synthetic event:
```bash
stripe trigger checkout.session.completed
```
On `checkout.session.completed`, Plutus credits the org's ledger and the
**Credit balance** card updates on the next 5-second refresh. "Upgrade to Pro"
moves the org to the `pro` tier; the Customer Portal button manages the
subscription.

## 5. Go live
Swap `sk_test_…`/`pk_test_…`/`whsec_…` for live keys, register a production
webhook endpoint in the Stripe dashboard pointing at
`https://your-host/webhook/stripe`, and re-run `plutus stripe-setup` once
against the live key to create the live price.

## How it's safe
- Webhooks are **signature-verified** and recorded by event id — a replay never
  double-credits (`stripe_events` table).
- The credit balance is the **sum of an append-only ledger**, so it's auditable
  and can't silently drift.
- No Stripe key → Checkout is simply disabled and everything else runs offline.
