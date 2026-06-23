#!/usr/bin/env python3
"""Integration smoke test: boot the HTTP server on an ephemeral port."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, demo
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=120)
        conn.close()

        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=True)
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

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()

    def test_healthz(self):
        status, body = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_dashboard_renders(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Plutus", body)
        self.assertIn("Credit balance", body)
        self.assertIn("Spend by workspace", body)
        self.assertIn("#0c0814", body)  # the brand bg color is present

    def test_pricing_page_public(self):
        status, body = self._get("/pricing")
        self.assertEqual(status, 200)
        for name in ("Free", "Pro", "Enterprise"):
            self.assertIn(name, body)
        self.assertIn("$20", body)
        self.assertIn("Contact sales", body)

    def test_api_summary(self):
        status, body = self._get("/api/summary")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertIn("balance", d)
        self.assertIn("by_provider", d)
        self.assertGreater(len(d["by_provider"]), 0)

    def test_api_orgs(self):
        status, body = self._get("/api/orgs")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(json.loads(body)), 1)

    def test_404(self):
        try:
            self._get("/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
