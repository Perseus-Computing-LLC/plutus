#!/usr/bin/env python3
"""#60: refunds, disputes, and failed payments reverse the ledger.

Before the fix, `handle_webhook_event` only handled checkout + subscription
events, so a refunded or charged-back top-up left the credit on Plutus's
append-only ledger forever — the org kept spending money already returned.

Reversals converge to a target cumulative amount per Stripe reference, so a
partial-then-full refund, a dispute fired twice (created + funds_withdrawn), and
replayed events all reverse exactly once."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db
from plutus_agent.billing import handle_webhook_event


class TestReversals(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Acme")["id"]
        db.set_stripe_customer(self.conn, self.org, "cus_123")
        # $50 prepaid top-up, keyed (as the real checkout path now does) by the
        # PaymentIntent so a dispute can map back to it.
        db.add_ledger(self.conn, self.org, 50.0, "topup",
                      reason="seed", stripe_ref="pi_1")

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def _evt(self, event_id, etype, obj):
        return handle_webhook_event(self.conn, {"id": event_id, "type": etype,
                                                "data": {"object": obj}})

    def _refund(self, event_id, amount_refunded, charge="ch_1"):
        return self._evt(event_id, "charge.refunded",
                         {"id": charge, "customer": "cus_123",
                          "payment_intent": "pi_1",
                          "amount_refunded": amount_refunded})

    def test_full_refund_zeroes_balance(self):
        res = self._refund("evt_r1", 5000)
        self.assertEqual(res["status"], "refunded")
        self.assertAlmostEqual(res["amount_usd"], 50.0, places=2)
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 0.0, places=2)

    def test_partial_then_full_refund_converges(self):
        self._refund("evt_p1", 2000)  # partial $20
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 30.0, places=2)
        # second event for the same charge: cumulative refunded is now $50
        res = self._refund("evt_p2", 5000)
        self.assertAlmostEqual(res["amount_usd"], 30.0, places=2)  # only the delta
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 0.0, places=2)

    def test_replayed_refund_event_is_duplicate(self):
        self._refund("evt_same", 5000)
        res = self._refund("evt_same", 5000)  # exact replay (same event id)
        self.assertEqual(res["status"], "duplicate")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 0.0, places=2)

    def test_distinct_events_same_cumulative_dont_double_reverse(self):
        # Two *different* events both reporting the same cumulative refund must
        # not reverse twice (second converges to a zero delta).
        self._refund("evt_a", 5000)
        res = self._refund("evt_b", 5000)
        self.assertEqual(res["status"], "noop")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 0.0, places=2)

    def test_dispute_reverses_via_payment_intent(self):
        # Dispute objects carry no customer — only the payment_intent — so the
        # org is mapped via the top-up's PaymentIntent ref.
        res = self._evt("evt_d1", "charge.dispute.created",
                        {"id": "dp_1", "payment_intent": "pi_1", "amount": 5000})
        self.assertEqual(res["status"], "disputed")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 0.0, places=2)

    def test_dispute_created_then_funds_withdrawn_reverses_once(self):
        self._evt("evt_d_a", "charge.dispute.created",
                  {"id": "dp_2", "payment_intent": "pi_1", "amount": 5000})
        res = self._evt("evt_d_b", "charge.dispute.funds_withdrawn",
                        {"id": "dp_2", "payment_intent": "pi_1", "amount": 5000})
        self.assertEqual(res["status"], "noop")  # already reversed by the delta
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 0.0, places=2)

    def test_unmappable_refund_is_no_op(self):
        res = self._evt("evt_x", "charge.refunded",
                        {"id": "ch_x", "customer": "cus_unknown",
                         "amount_refunded": 5000})
        self.assertEqual(res["status"], "no_org")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 50.0, places=2)

    def test_payment_failed_logs_alert_without_touching_balance_or_tier(self):
        tier_before = db.get_org(self.conn, self.org)["tier"]
        res = self._evt("evt_f1", "invoice.payment_failed",
                        {"customer": "cus_123"})
        self.assertEqual(res["status"], "payment_failed")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 50.0, places=2)
        self.assertEqual(db.get_org(self.conn, self.org)["tier"], tier_before)
        alerts = db.recent_alerts(self.conn, self.org)
        self.assertTrue(any(a["kind"] == "payment_failed" for a in alerts))


if __name__ == "__main__":
    unittest.main()
