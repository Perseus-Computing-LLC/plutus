#!/usr/bin/env python3
"""#61: a negative ``cost_usd`` must never reach the metering debit hot path.

Without the guard, ``record_usage`` passed ``-cost_usd`` to
``db.add_ledger(..., "debit")`` where ``-(-x)`` is a *positive* delta — minting
prepaid credit out of thin air — and the prepaid hard-stop (``balance - cost_usd
< 0``) could never trip because a negative cost only *raises* the projected
balance. The fix rejects negatives at the metering core (ValueError) and at the
``/v1/usage`` HTTP boundary (400)."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app


class TestCoreGuard(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Acme", tier="pro")["id"]
        db.add_ledger(self.conn, self.org, 1.0, "topup")  # $1 credit

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_negative_cost_raises(self):
        with self.assertRaises(ValueError):
            metering.record_usage(self.conn, self.org, provider="anthropic",
                                  cost_usd=-5.0)

    def test_negative_cost_never_mints_credit(self):
        before = db.get_balance(self.conn, self.org)
        with self.assertRaises(ValueError):
            metering.record_usage(self.conn, self.org, provider="anthropic",
                                  cost_usd=-100.0)
        after = db.get_balance(self.conn, self.org)
        # Balance must be unchanged — no ledger entry, no minted credit.
        self.assertEqual(before, after)
        self.assertLessEqual(after, 1.0)

    def test_zero_cost_still_records(self):
        res = metering.record_usage(self.conn, self.org, provider="anthropic",
                                    cost_usd=0.0)
        self.assertTrue(res.recorded)
        self.assertEqual(res.cost_usd, 0.0)


class TestHttpBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Prepaid Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        db.add_ledger(conn, cls.org_id, 1.0, "topup")
        conn.close()
        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, payload):
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        return urllib.request.urlopen(req, timeout=5)

    def test_negative_cost_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post({"provider": "anthropic", "cost_usd": -10.0})
        self.assertEqual(cm.exception.code, 400)

    def test_non_numeric_cost_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post({"provider": "anthropic", "cost_usd": "free"})
        self.assertEqual(cm.exception.code, 400)

    def test_negative_in_batch_rejects_whole_batch(self):
        # Fix #27 semantics: one bad event rejects the batch before any record.
        before = db.connect(self.dbpath)
        bal_before = db.get_balance(before, self.org_id)
        before.close()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post([
                {"provider": "anthropic", "cost_usd": 0.01},
                {"provider": "anthropic", "cost_usd": -50.0},
            ])
        self.assertEqual(cm.exception.code, 400)
        after = db.connect(self.dbpath)
        bal_after = db.get_balance(after, self.org_id)
        after.close()
        self.assertEqual(bal_before, bal_after)


if __name__ == "__main__":
    unittest.main()
