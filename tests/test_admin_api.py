#!/usr/bin/env python3
"""#66: token-scoped admin API under /v1/admin for scripting tenant management."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app

ADMIN_TOKEN = "tok_admin_secret"


def _start(admin_token=ADMIN_TOKEN):
    fd, dbpath = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(dbpath)
    db.init_schema(conn)
    conn.close()
    cfg = dict(DEFAULT_CONFIG)
    cfg["admin"] = {"token": admin_token}
    ctx = app._Ctx(cfg, dbpath, demo=False)
    httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, dbpath


class _Base(unittest.TestCase):
    def _req(self, method, path, body=None, token=ADMIN_TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=data, headers=headers, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())


class TestAdminAuth(_Base):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port, cls.dbpath = _start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def test_wrong_token_401(self):
        s, _ = self._req("GET", "/v1/admin/orgs", token="nope")
        self.assertEqual(s, 401)

    def test_missing_token_401(self):
        s, _ = self._req("GET", "/v1/admin/orgs", token=None)
        self.assertEqual(s, 401)


class TestAdminDisabled(_Base):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port, cls.dbpath = _start(admin_token="")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def test_disabled_returns_404(self):
        # No token configured → admin API is invisible.
        s, _ = self._req("GET", "/v1/admin/orgs", token="anything")
        self.assertEqual(s, 404)


class TestAdminOps(_Base):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port, cls.dbpath = _start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def test_org_credit_key_lifecycle(self):
        # create org
        s, org = self._req("POST", "/v1/admin/orgs", {"name": "Acme", "tier": "pro"})
        self.assertEqual(s, 201)
        org_id = org["id"]
        self.assertEqual(org["tier"], "pro")

        # it shows up in the list
        s, listing = self._req("GET", "/v1/admin/orgs")
        self.assertEqual(s, 200)
        self.assertIn(org_id, [o["id"] for o in listing["orgs"]])

        # grant credit
        s, res = self._req("POST", "/v1/admin/credits",
                           {"org_id": org_id, "amount_usd": 25.0, "kind": "grant"})
        self.assertEqual(s, 201)
        self.assertAlmostEqual(res["balance_after"], 25.0, places=2)

        # adjust (debit) credit
        s, res = self._req("POST", "/v1/admin/credits",
                           {"org_id": org_id, "amount_usd": -5.0, "kind": "adjust"})
        self.assertEqual(s, 201)
        self.assertAlmostEqual(res["balance_after"], 20.0, places=2)

        # create a key — secret returned once
        s, key = self._req("POST", "/v1/admin/keys", {"org_id": org_id, "name": "ci"})
        self.assertEqual(s, 201)
        self.assertTrue(key["secret"])

        # list keys — secret is NOT echoed
        s, keys = self._req("GET", f"/v1/admin/keys?org={org_id}")
        self.assertEqual(s, 200)
        self.assertEqual(len(keys["keys"]), 1)
        self.assertNotIn("secret", keys["keys"][0])

    def test_grant_negative_rejected(self):
        s, org = self._req("POST", "/v1/admin/orgs", {"name": "Neg"})
        s, res = self._req("POST", "/v1/admin/credits",
                           {"org_id": org["id"], "amount_usd": -1.0, "kind": "grant"})
        self.assertEqual(s, 400)

    def test_credit_unknown_org_404(self):
        s, _ = self._req("POST", "/v1/admin/credits",
                         {"org_id": "nope", "amount_usd": 1.0})
        self.assertEqual(s, 404)

    def test_create_org_requires_name(self):
        s, _ = self._req("POST", "/v1/admin/orgs", {"tier": "free"})
        self.assertEqual(s, 400)


if __name__ == "__main__":
    unittest.main()
