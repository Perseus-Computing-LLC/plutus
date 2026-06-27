#!/usr/bin/env python3
"""#65: ingest/operational hardening — Idempotency-Key, per-key rate limit, and
the locked-down monitor-bridge subprocess."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import bridge, db
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app


def _server(cfg_mut=None):
    fd, dbpath = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(dbpath)
    db.init_schema(conn)
    org = db.create_org(conn, "Acme", tier="pro")["id"]
    _, key = db.create_api_key(conn, org)
    db.add_ledger(conn, org, 100.0, "topup")
    conn.close()
    cfg = dict(DEFAULT_CONFIG)
    if cfg_mut:
        cfg_mut(cfg)
    ctx = app._Ctx(cfg, dbpath, demo=False)
    httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, key, org, dbpath


class TestIdempotency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port, cls.key, cls.org, cls.dbpath = _server()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, payload, idem=None):
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.key}"}
        if idem:
            headers["Idempotency-Key"] = idem
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/usage",
            data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _balance(self):
        conn = db.connect(self.dbpath)
        try:
            return db.get_balance(conn, self.org)
        finally:
            conn.close()

    def test_duplicate_key_does_not_double_charge(self):
        ev = {"provider": "anthropic", "cost_usd": 1.0}
        s1, b1 = self._post(ev, idem="abc-1")
        bal_after_first = self._balance()
        s2, b2 = self._post(ev, idem="abc-1")  # retry, same key
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        self.assertTrue(b2.get("idempotent_replay"))
        # balance unchanged by the replay
        self.assertAlmostEqual(self._balance(), bal_after_first, places=6)

    def test_distinct_keys_both_charge(self):
        before = self._balance()
        self._post({"provider": "anthropic", "cost_usd": 1.0}, idem="k-a")
        self._post({"provider": "anthropic", "cost_usd": 1.0}, idem="k-b")
        self.assertAlmostEqual(self._balance(), before - 2.0, places=6)

    def test_no_key_behaves_normally(self):
        s, b = self._post({"provider": "anthropic", "cost_usd": 0.5})
        self.assertEqual(s, 200)
        self.assertTrue(b["recorded"])


class TestRateLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Tiny bucket so the test trips it deterministically.
        def mut(cfg):
            cfg["ingest"] = {"rate_per_min": 60, "burst": 2}
        cls.httpd, cls.port, cls.key, cls.org, cls.dbpath = _server(mut)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/usage",
            data=json.dumps({"provider": "anthropic", "cost_usd": 0.001}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_burst_then_429(self):
        # burst=2 → first two pass, third is limited.
        self.assertEqual(self._post(), 200)
        self.assertEqual(self._post(), 200)
        self.assertEqual(self._post(), 429)


class TestBridgeLockdown(unittest.TestCase):
    def test_disabled_returns_none(self):
        self.assertIsNone(bridge.runway({"enabled": False, "command": "/bin/x"}))

    def test_relative_binary_refused(self):
        cfg = {"enabled": True, "command": "python3 foo.py",
               "allowed_binaries": ["python3"]}
        self.assertIsNone(bridge.runway(cfg))

    def test_absolute_but_not_allowlisted_refused(self):
        cfg = {"enabled": True, "command": "/usr/bin/python3 foo.py",
               "allowed_binaries": []}
        self.assertIsNone(bridge.runway(cfg))

    def test_absolute_but_other_binary_refused(self):
        cfg = {"enabled": True, "command": "/usr/bin/python3 foo.py",
               "allowed_binaries": ["/opt/plutus/run"]}
        self.assertIsNone(bridge.runway(cfg))


class TestRunwayAuthGate(unittest.TestCase):
    def test_unauthenticated_does_not_shell_out_when_auth_on(self):
        cfg = dict(DEFAULT_CONFIG)
        cfg["monitor"] = {"enabled": True, "command": "/bin/true",
                          "allowed_binaries": ["/bin/true"]}
        ctx = app._Ctx(cfg, ":memory:", demo=False)
        ctx.auth_on = True
        ctx._runway = {"cached": True}
        # An unauthenticated request returns the cached value without executing.
        self.assertEqual(ctx.runway(authed=False), {"cached": True})


if __name__ == "__main__":
    unittest.main()
