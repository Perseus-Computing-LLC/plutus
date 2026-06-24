"""Stripe client — Checkout for prepaid credits + Pro subscription, Customer
Portal, and webhook handling.

Stripe is **optional**. With no secret key the client reports ``available ==
False`` and every method raises :class:`BillingError` with a clear message, so
the dashboard can show a "connect Stripe to enable billing" state while every
non-Stripe feature keeps working offline. With a *test-mode* key
(``sk_test_...``) the full flow works end-to-end against Stripe's test
environment.

Two purchase paths:

* **Prepaid credits** — a one-time Checkout Session (``mode=payment``) for a
  chosen dollar amount. On ``checkout.session.completed`` we top up the org's
  credit ledger by the amount paid.
* **Pro plan** — a subscription Checkout Session (``mode=subscription``) against
  the configured Price. Subscription lifecycle webhooks move the org between the
  ``pro`` and ``free`` tiers.

Webhook handling is **idempotent**: every event id is recorded in
``stripe_events`` and never applied twice.
"""
from __future__ import annotations

from typing import Optional

from .. import db


class BillingError(RuntimeError):
    pass


def _load_stripe():
    try:
        import stripe  # type: ignore
        return stripe
    except ImportError:
        return None


class StripeClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.billing = self.cfg.get("billing", {})
        self.secret = self.billing.get("stripe_secret_key") or ""
        self.publishable = self.billing.get("stripe_publishable_key") or ""
        self.webhook_secret = self.billing.get("stripe_webhook_secret") or ""
        self.currency = (self.billing.get("currency") or "usd").lower()
        self._stripe = _load_stripe()
        if self._stripe and self.secret:
            self._stripe.api_key = self.secret

    # ------------------------------------------------------------- status ---
    @property
    def available(self) -> bool:
        return bool(self._stripe and self.secret)

    @property
    def test_mode(self) -> bool:
        return self.secret.startswith("sk_test_")

    def status(self) -> dict:
        if not self._stripe:
            mode = "offline (stripe SDK not installed)"
        elif not self.secret:
            mode = "offline (no API key)"
        elif self.test_mode:
            mode = "test mode"
        else:
            mode = "live mode"
        return {
            "available": self.available,
            "test_mode": self.test_mode,
            "mode": mode,
            "publishable_key": self.publishable,
            "has_pro_price": bool(self.billing.get("stripe_price_pro")),
        }

    def _require(self):
        if not self._stripe:
            raise BillingError(
                "Stripe SDK not installed. `pip install stripe` to enable billing."
            )
        if not self.secret:
            raise BillingError(
                "No Stripe key configured. Set billing.stripe_secret_key or "
                "the STRIPE_SECRET_KEY env var."
            )

    # ----------------------------------------------------------- customer ---
    def ensure_customer(self, conn, org_id: str) -> str:
        """Get-or-create a Stripe customer for an org; persist the id."""
        self._require()
        org = db.get_org(conn, org_id)
        if org is None:
            raise BillingError(f"unknown org {org_id}")
        if org["stripe_customer_id"]:
            return org["stripe_customer_id"]
        owner = conn.execute(
            "SELECT email FROM users WHERE org_id=? ORDER BY created_at LIMIT 1",
            (org_id,),
        ).fetchone()
        cust = self._stripe.Customer.create(
            name=org["name"],
            email=owner["email"] if owner else None,
            metadata={"plutus_org_id": org_id},
        )
        db.set_stripe_customer(conn, org_id, cust["id"])
        return cust["id"]

    # ----------------------------------------------------------- checkout ---
    def credit_checkout(self, conn, org_id: str, amount_usd: float) -> dict:
        """One-time Checkout Session to buy ``amount_usd`` of prepaid credit."""
        self._require()
        if amount_usd <= 0:
            raise BillingError("amount must be positive")
        customer = self.ensure_customer(conn, org_id)
        session = self._stripe.checkout.Session.create(
            mode="payment",
            customer=customer,
            line_items=[{
                "price_data": {
                    "currency": self.currency,
                    "unit_amount": int(round(amount_usd * 100)),
                    "product_data": {
                        "name": "Plutus prepaid credit",
                        "description": f"${amount_usd:,.2f} of agent spend credit",
                    },
                },
                "quantity": 1,
            }],
            success_url=self.billing.get("success_url", ""),
            cancel_url=self.billing.get("cancel_url", ""),
            metadata={"plutus_org_id": org_id, "kind": "credit",
                      "amount_usd": f"{amount_usd:.2f}"},
        )
        return {"id": session["id"], "url": session["url"]}

    def pro_checkout(self, conn, org_id: str) -> dict:
        """Subscription Checkout Session for the $20/mo Pro plan."""
        self._require()
        price = self.billing.get("stripe_price_pro")
        if not price:
            raise BillingError(
                "No Pro price configured. Set billing.stripe_price_pro to the "
                "Stripe Price ID for the $20/mo plan."
            )
        customer = self.ensure_customer(conn, org_id)
        session = self._stripe.checkout.Session.create(
            mode="subscription",
            customer=customer,
            line_items=[{"price": price, "quantity": 1}],
            success_url=self.billing.get("success_url", ""),
            cancel_url=self.billing.get("cancel_url", ""),
            metadata={"plutus_org_id": org_id, "kind": "subscription"},
        )
        return {"id": session["id"], "url": session["url"]}

    def portal(self, conn, org_id: str, return_url: Optional[str] = None) -> dict:
        """Stripe Customer Portal session for self-serve billing management."""
        self._require()
        customer = self.ensure_customer(conn, org_id)
        session = self._stripe.billing_portal.Session.create(
            customer=customer,
            return_url=return_url or self.billing.get("success_url", ""),
        )
        return {"url": session["url"]}

    # ------------------------------------------------------------ webhook ---
    def construct_event(self, payload: bytes, sig_header: str):
        """Verify + parse a webhook payload into a Stripe event object."""
        self._require()
        if not self.webhook_secret:
            raise BillingError("No webhook secret configured "
                               "(billing.stripe_webhook_secret).")
        return self._stripe.Webhook.construct_event(
            payload, sig_header, self.webhook_secret
        )


# --------------------------------------------------------- webhook applying ---
def handle_webhook_event(conn, event: dict) -> dict:
    """Apply a verified Stripe event to the database (idempotent).

    ``event`` is a dict-like Stripe Event (``event["type"]``, ``event["data"]
    ["object"]``, ``event["id"]``). Returns a small summary for logging.
    """
    event_id = event.get("id", "")
    etype = event.get("type", "")
    
    # Atomically claim the event first (fix #26: prevent concurrent double-credit)
    if event_id and not db.mark_stripe_event(conn, event_id, etype):
        return {"status": "duplicate", "type": etype, "id": event_id}

    obj = (event.get("data") or {}).get("object") or {}
    result = {"status": "ignored", "type": etype, "id": event_id}

    try:
        if etype == "checkout.session.completed":
            result = _apply_checkout_completed(conn, obj)
        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            result = _apply_subscription_change(conn, obj)
        elif etype == "customer.subscription.deleted":
            result = _apply_subscription_deleted(conn, obj)
    except Exception:
        # Rollback the event claim so it can be retried
        if event_id:
            db.unmark_stripe_event(conn, event_id)
        raise

    result.setdefault("type", etype)
    result.setdefault("id", event_id)
    return result


def _org_from_metadata_or_customer(conn, obj) -> Optional[str]:
    meta = obj.get("metadata") or {}
    org_id = meta.get("plutus_org_id")
    if org_id and db.get_org(conn, org_id):
        return org_id
    customer = obj.get("customer")
    if customer:
        row = db.org_by_stripe_customer(conn, customer)
        if row:
            return row["id"]
    return None


def _apply_checkout_completed(conn, obj) -> dict:
    org_id = _org_from_metadata_or_customer(conn, obj)
    if not org_id:
        return {"status": "no_org", "detail": "could not map checkout to an org"}
    meta = obj.get("metadata") or {}
    kind = meta.get("kind")
    if kind == "credit" or obj.get("mode") == "payment":
        # Fix #29: prefer Stripe's collected amount_total over client-supplied metadata
        amount_total = obj.get("amount_total")
        if amount_total is not None:
            usd = float(amount_total) / 100.0
        else:
            amount = meta.get("amount_usd")
            usd = float(amount) if amount is not None else 0.0
        row = db.add_ledger(conn, org_id, usd, "topup",
                            reason="Stripe checkout", stripe_ref=obj.get("id"))
        return {"status": "credited", "org_id": org_id, "amount_usd": usd,
                "balance_after": float(row["balance_after"])}
    if kind == "subscription" or obj.get("mode") == "subscription":
        db.set_org_tier(conn, org_id, "pro")
        return {"status": "subscribed", "org_id": org_id, "tier": "pro"}
    return {"status": "ignored", "org_id": org_id}


def _apply_subscription_change(conn, obj) -> dict:
    org_id = _org_from_metadata_or_customer(conn, obj)
    if not org_id:
        return {"status": "no_org"}
    status = obj.get("status")
    tier = "pro" if status in ("active", "trialing", "past_due") else "free"
    db.set_org_tier(conn, org_id, tier)
    return {"status": "tier_set", "org_id": org_id, "tier": tier,
            "subscription_status": status}


def _apply_subscription_deleted(conn, obj) -> dict:
    org_id = _org_from_metadata_or_customer(conn, obj)
    if not org_id:
        return {"status": "no_org"}
    db.set_org_tier(conn, org_id, "free")
    return {"status": "downgraded", "org_id": org_id, "tier": "free"}
