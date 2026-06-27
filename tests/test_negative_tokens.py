#!/usr/bin/env python3
"""#80: a negative token count must never reach the meter.

Without the guard, negative `input_tokens` (with a non-negative cost_usd, so the
#61 guard didn't trip) rewound `tracked_tokens_mtd` — bypassing the free-tier
quota — and corrupted every SUM(tokens) aggregate."""
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
        self.org = db.create_org(self.conn, "Acme", tier="free")["id"]

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_negative_tokens_raise(self):
        with self.assertRaises(ValueError):
            metering.record_usage(self.conn, self.org, provider="anthropic",
                                  input_tokens=-8000, cost_usd=0.0)

    def test_meter_not_rewound(self):
        metering.record_usage(self.conn, self.org, provider="anthropic",
                              input_tokens=9000, cost_usd=0.0)
        before = metering.tracked_tokens_mtd(self.conn, self.org)
        with self.assertRaises(ValueError):
            metering.record_usage(self.conn, self.org, provider="anthropic",
                                  input_tokens=-8000, cost_usd=0.0)
        after = metering.tracked_tokens_mtd(self.conn, self.org)
        self.assertEqual(before, after)
        self.assertEqual(after, 9000)


class TestHttpBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org = db.create_org(conn, "Acme", tier="free")["id"]
        _, cls.key = db.create_api_key(conn, cls.org)
        conn.close()
        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/usage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_negative_input_tokens_400(self):
        self.assertEqual(
            self._post({"provider": "anthropic", "input_tokens": -5, "cost_usd": 0.0}), 400)

    def test_negative_in_batch_rejects_whole_batch(self):
        before = db.connect(self.dbpath)
        n_before = metering.tracked_tokens_mtd(before, self.org)
        before.close()
        code = self._post([
            {"provider": "anthropic", "input_tokens": 100},
            {"provider": "anthropic", "input_tokens": -100},
        ])
        self.assertEqual(code, 400)
        after = db.connect(self.dbpath)
        n_after = metering.tracked_tokens_mtd(after, self.org)
        after.close()
        self.assertEqual(n_before, n_after)


if __name__ == "__main__":
    unittest.main()
