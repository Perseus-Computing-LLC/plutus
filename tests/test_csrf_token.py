#!/usr/bin/env python3
"""#58: per-session CSRF synchronizer token as defense-in-depth.

The existing fail-closed Origin/Referer check (#32) rejects a legit request when
a privacy proxy strips those headers. A valid CSRF token now lets such a request
through, while a forgery (no token, no origin) is still blocked."""
import copy
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, demo
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app, auth


def _auth_cfg():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["auth"].update({
        "enabled": True,
        "google_client_id": "cid.apps.googleusercontent.com",
        "google_client_secret": "secret",
        "base_url": "http://127.0.0.1",
    })
    return cfg


class TestCsrfToken(unittest.TestCase):
    def test_token_is_deterministic_and_session_bound(self):
        t1 = auth.csrf_token("sess-abc")
        self.assertEqual(t1, auth.csrf_token("sess-abc"))
        self.assertNotEqual(t1, auth.csrf_token("sess-xyz"))
        self.assertNotEqual(t1, "sess-abc")  # never leaks the session token
        self.assertEqual(auth.csrf_token(""), "")


class TestCsrfEnforcement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=5)
        cls.user = db.ensure_user(conn, cls.org_id, "owner@example.com",
                                  name="Owner", role="owner")
        cls.token = db.create_session(conn, cls.user["id"], ttl_seconds=3600)
        conn.close()
        ctx = app._Ctx(_auth_cfg(), cls.dbpath, demo=True)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        ctx.cfg["auth"]["base_url"] = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.csrf = auth.csrf_token(cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, path, data=b"", origin=None):
        headers = {"Cookie": f"plutus_session={self.token}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=data, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=5).status, ""
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_valid_token_without_origin_passes(self):
        # No Origin header (simulating a stripping proxy) + valid token → the
        # CSRF gate is cleared and the request reaches the handler (not 403).
        data = f"org={self.org_id}&_csrf={self.csrf}".encode()
        status, body = self._post("/keys/create", data=data)
        self.assertNotEqual(status, 403)
        self.assertNotIn("cross-origin", body)

    def test_missing_token_without_origin_blocked(self):
        status, body = self._post("/keys/create",
                                  data=f"org={self.org_id}".encode())
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", body)

    def test_wrong_token_without_origin_blocked(self):
        data = f"org={self.org_id}&_csrf=deadbeef".encode()
        status, body = self._post("/keys/create", data=data)
        self.assertEqual(status, 403)

    def test_same_origin_still_passes_without_token(self):
        status, body = self._post("/keys/create",
                                  data=f"org={self.org_id}".encode(),
                                  origin=f"http://127.0.0.1:{self.port}")
        self.assertNotEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
