#!/usr/bin/env python3
"""#63: four independent money-correctness fixes.

1. currency is enforced USD-only (the ledger has no currency dimension);
2. `past_due` no longer counts as active Pro;
3. credit checkout amounts are bounded ($1–$10,000, finite);
4. month boundaries are computed in UTC, matching the UTC-epoch event store."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering
from plutus_agent.billing import handle_webhook_event, BillingError
from plutus_agent.billing.stripe_client import StripeClient
from plutus_agent.server.app import _parse_credit_amount


class TestUtcMonthFloor(unittest.TestCase):
    def test_month_floor_is_utc(self):
        # A mid-month UTC instant must floor to UTC midnight on the 1st,
        # independent of the server's local timezone.
        ts = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()
        expected = dt.datetime(2026, 3, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(metering._month_floor(ts), expected)

    def test_first_of_month_just_after_midnight_utc(self):
        ts = dt.datetime(2026, 1, 1, 0, 30, tzinfo=dt.timezone.utc).timestamp()
        expected = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(metering._month_floor(ts), expected)


class TestPastDueNotPro(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Acme", tier="pro")["id"]
        db.set_stripe_customer(self.conn, self.org, "cus_123")

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_past_due_downgrades_to_free(self):
        handle_webhook_event(self.conn, {
            "id": "evt_pd", "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_123", "status": "past_due"}}})
        self.assertEqual(db.get_org(self.conn, self.org)["tier"], "free")

    def test_active_restores_pro(self):
        db.set_org_tier(self.conn, self.org, "free")
        handle_webhook_event(self.conn, {
            "id": "evt_act", "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_123", "status": "active"}}})
        self.assertEqual(db.get_org(self.conn, self.org)["tier"], "pro")


class TestCreditAmountBounds(unittest.TestCase):
    def test_default_when_blank(self):
        self.assertEqual(_parse_credit_amount(None), 50.0)
        self.assertEqual(_parse_credit_amount(""), 50.0)

    def test_valid_amount(self):
        self.assertEqual(_parse_credit_amount("100"), 100.0)

    def test_rejects_non_numeric(self):
        with self.assertRaises(BillingError):
            _parse_credit_amount("free-money")

    def test_rejects_non_finite(self):
        for bad in ("inf", "-inf", "nan"):
            with self.assertRaises(BillingError):
                _parse_credit_amount(bad)

    def test_rejects_out_of_range(self):
        for bad in ("0", "0.5", "10001", "1000000000"):
            with self.assertRaises(BillingError):
                _parse_credit_amount(bad)


class TestUsdOnly(unittest.TestCase):
    def _client(self, currency):
        c = StripeClient({"billing": {"stripe_secret_key": "sk_test_x",
                                      "currency": currency}})
        c._stripe = object()  # pretend the SDK is installed so _require gets past it
        return c

    def test_non_usd_currency_rejected(self):
        with self.assertRaises(BillingError) as cm:
            self._client("eur")._require()
        self.assertIn("USD", str(cm.exception))

    def test_usd_currency_ok(self):
        # Should not raise on the currency check (SDK + key are stubbed/present).
        self._client("usd")._require()


if __name__ == "__main__":
    unittest.main()
