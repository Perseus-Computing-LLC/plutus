#!/usr/bin/env python3
"""Regression coverage for the 1.0 low-severity punch-list (issue #37) and the
related OIDC unsigned-token gate."""
import copy
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import cli, config as cfgmod, db, demo
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app, auth as authmod


# --------------------------------------------------------------------- #37.1 ---
class TestCliNameValidation(unittest.TestCase):
    """`org create` / `workspace create` with no NAME must exit cleanly, not
    crash in slugify(None)."""

    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["PLUTUS_DB"] = self.dbpath
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        self.org_id = db.create_org(conn, "Acme")["id"]
        conn.close()

    def tearDown(self):
        os.environ.pop("PLUTUS_DB", None)
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_org_create_without_name_exits(self):
        args = types.SimpleNamespace(action="create", name=None,
                                     tier="free", email=None)
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_org(args)
        self.assertIn("NAME", str(cm.exception))

    def test_workspace_create_without_name_exits(self):
        args = types.SimpleNamespace(action="create", name="  ",
                                     org="Acme", budget=None)
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_workspace(args)
        self.assertIn("NAME", str(cm.exception))


# ------------------------------------------------------------------- #37.4-5 ---
class TestAuthzAndKeyThrottle(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.a = db.create_org(self.conn, "Org A", owner_email="me@x.com")["id"]
        self.b = db.create_org(self.conn, "Org B", owner_email="other@x.com")["id"]
        # make me@x.com a member of both orgs
        db.ensure_user(self.conn, self.b, "me@x.com", role="member")

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def _handler(self):
        h = types.SimpleNamespace()
        h._user = db.users_by_email(self.conn, "me@x.com")[0]
        return h

    def test_strict_multi_org_requires_explicit(self):
        h = self._handler()
        with self.assertRaises(app._OrgRequired):
            app.Handler._authz_org(h, self.conn, None, strict=True)

    def test_non_strict_multi_org_defaults_first(self):
        h = self._handler()
        # lenient (dashboard GET) still resolves to a member org
        self.assertIn(app.Handler._authz_org(h, self.conn, None, strict=False),
                      {self.a, self.b})

    def test_strict_explicit_org_ok(self):
        h = self._handler()
        self.assertEqual(
            app.Handler._authz_org(h, self.conn, self.a, strict=True), self.a)

    def test_last_used_at_throttled(self):
        _, secret = db.create_api_key(self.conn, self.a)
        self.assertEqual(db.api_key_org(self.conn, secret), self.a)
        first = db.get_api_key(
            self.conn,
            db.list_api_keys(self.conn, self.a)[0]["id"])["last_used_at"]
        self.assertIsNotNone(first)
        # a second auth within the throttle window must not rewrite the column
        db.api_key_org(self.conn, secret)
        kid = db.list_api_keys(self.conn, self.a)[0]["id"]
        self.assertEqual(db.get_api_key(self.conn, kid)["last_used_at"], first)
        # backdate it past the window → next auth updates it
        self.conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?",
                          (first - 120, kid))
        self.conn.commit()
        db.api_key_org(self.conn, secret)
        self.assertGreater(db.get_api_key(self.conn, kid)["last_used_at"],
                           first - 120)


# --------------------------------------------------------------------- #37.6 ---
class TestInstallHookBackup(unittest.TestCase):
    def test_backup_preserves_original_bytes_and_is_not_clobbered(self):
        d = tempfile.mkdtemp()
        try:
            settings = Path(d) / "settings.json"
            # compact, hand-formatted original — the old code re-serialized this
            # (losing formatting) and overwrote the backup on every run.
            original = b'{"hooks":{"Stop":[]},"_note":"keep me"}'
            settings.write_bytes(original)
            args = types.SimpleNamespace(path=str(settings), print=False)
            cli.cmd_install_hook(args)
            backup = settings.with_suffix(".json.plutus-bak")
            self.assertTrue(backup.exists())
            self.assertEqual(backup.read_bytes(), original)
            # second run: file is now modified; the pristine backup must survive
            cli.cmd_install_hook(args)
            self.assertEqual(backup.read_bytes(), original)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------- #37.8 ---
class TestMinimalYamlRoundTrip(unittest.TestCase):
    """The PyYAML-free fallback reader must read back the block style PyYAML
    writes — including non-empty lists and one level of nesting."""

    def test_lists_and_nesting_survive(self):
        d = tempfile.mkdtemp()
        try:
            path = Path(d) / "config.yaml"
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg["auth"]["allowed_emails"] = ["a@b.com", "c@d.com"]
            cfg["auth"]["allowed_domain"] = "example.com"
            cfg["alerts"]["to_addrs"] = ["ops@x.com"]
            cfgmod._dump_yaml(path, cfg)            # written via PyYAML
            got = cfgmod._minimal_yaml_read(path)   # read without PyYAML
            self.assertEqual(got["auth"]["allowed_emails"], ["a@b.com", "c@d.com"])
            self.assertEqual(got["auth"]["allowed_domain"], "example.com")
            self.assertEqual(got["alerts"]["to_addrs"], ["ops@x.com"])
            self.assertEqual(got["server"]["port"], 8420)
            self.assertEqual(got["auth"]["enabled"], False)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------- OIDC hdr gate ---
class TestUnsignedTokenGate(unittest.TestCase):
    """The old `parts[0] == "hdr"` magic string must no longer bypass signature
    verification; only the explicit, default-off config flag does."""

    def _token(self):
        import base64
        claims = {"aud": "cid", "iss": "https://accounts.google.com",
                  "exp": time.time() + 3600, "nonce": "n",
                  "email": "x@y.com", "email_verified": True}
        payload = base64.urlsafe_b64encode(
            json.dumps(claims).encode()).rstrip(b"=").decode()
        return f"hdr.{payload}.sig"

    def test_hdr_no_longer_bypasses(self):
        cfg = {"auth": {"google_client_id": "cid", "allow_unsigned_tokens": False}}
        with self.assertRaises(authmod.AuthError):
            authmod._claims_from_id_token(self._token(), cfg, "n")

    def test_flag_allows_unsigned(self):
        cfg = {"auth": {"google_client_id": "cid", "allow_unsigned_tokens": True}}
        claims = authmod._claims_from_id_token(self._token(), cfg, "n")
        self.assertEqual(claims["email"], "x@y.com")

    def test_default_config_flag_off(self):
        self.assertFalse(DEFAULT_CONFIG["auth"]["allow_unsigned_tokens"])


# ----------------------------------------------------- server 404 / 500 sinks ---
class TestErrorSinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=10)
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

    def _raw_get(self, raw_path):
        """Send a GET whose request-line path contains literal characters
        (urllib would percent-encode them), so we can probe reflected XSS."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", self.port))
        s.sendall(f"GET {raw_path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  f"Connection: close\r\n\r\n".encode())
        chunks = []
        while True:
            b = s.recv(8192)
            if not b:
                break
            chunks.append(b)
        s.close()
        return b"".join(chunks).decode("utf-8", "replace")

    def test_404_path_is_escaped(self):
        resp = self._raw_get("/<script>alert(1)</script>")
        self.assertIn("404", resp.split("\r\n", 1)[0])
        self.assertNotIn("<script>alert(1)", resp)
        self.assertIn("&lt;script&gt;", resp)

    def test_500_does_not_leak_exception_text(self):
        sentinel = "SENSITIVE_DB_DETAIL_d34db33f"
        orig = app.api.summary_json
        app.api.summary_json = lambda *a, **k: (_ for _ in ()).throw(
            Exception(sentinel))
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                url = f"http://127.0.0.1:{self.port}/api/summary"
                urllib.request.urlopen(url, timeout=5)
            self.assertEqual(cm.exception.code, 500)
            body = cm.exception.read().decode()
            self.assertNotIn(sentinel, body)
            self.assertIn("Reference:", body)
        finally:
            app.api.summary_json = orig


if __name__ == "__main__":
    unittest.main()
