#!/usr/bin/env python3
"""Integration smoke test: boot the HTTP server on an ephemeral port."""
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import Meter, db, demo
from plutus_agent.client import PlutusAuthError, PlutusError
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=120)
        _, cls.key = db.create_api_key(conn, cls.org_id, name="test")
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

    def _post(self, path, payload, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

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

    # ---- ingest API ----------------------------------------------------------
    def test_ingest_requires_key(self):
        status, body = self._post("/v1/usage", {"provider": "anthropic"})
        self.assertEqual(status, 401)

    def test_ingest_bad_key_rejected(self):
        status, _ = self._post("/v1/usage", {"provider": "anthropic"},
                               token="plutus_sk_bogus")
        self.assertEqual(status, 401)

    def test_ingest_records_event(self):
        status, body = self._post("/v1/usage", {
            "provider": "anthropic", "model": "claude-opus-4-8",
            "input_tokens": 1200, "output_tokens": 800, "workspace": "prod",
        }, token=self.key)
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])
        self.assertTrue(body["event_id"].startswith("evt_"))
        self.assertGreater(body["cost_usd"], 0)
        self.assertEqual(body["org_id"], self.org_id)

    def test_ingest_missing_provider_400(self):
        status, _ = self._post("/v1/usage", {"input_tokens": 10}, token=self.key)
        self.assertEqual(status, 400)

    def test_ingest_batch(self):
        status, body = self._post("/v1/usage", [
            {"provider": "anthropic", "input_tokens": 100, "cost_usd": 0.01},
            {"provider": "google", "input_tokens": 200, "cost_usd": 0.02},
        ], token=self.key)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["recorded"], 2)

    # ---- SDK remote mode (Meter → /v1/usage) --------------------------------
    def _remote_meter(self, **kw):
        return Meter(remote=f"http://127.0.0.1:{self.port}", api_key=self.key, **kw)

    def test_remote_meter_records(self):
        m = self._remote_meter()
        self.assertTrue(m.is_remote)
        r = m.track(provider="anthropic", model="claude-opus-4-8",
                    input_tokens=1000, output_tokens=500, workspace="prod")
        self.assertTrue(r.recorded)
        self.assertTrue(r.event_id.startswith("evt_"))
        self.assertGreater(r.cost_usd, 0)
        m.close()

    def test_remote_meter_bad_key_raises(self):
        m = Meter(remote=f"http://127.0.0.1:{self.port}", api_key="plutus_sk_bogus")
        with self.assertRaises(PlutusAuthError):
            m.track(provider="anthropic", input_tokens=10)

    def test_remote_meter_no_key_errors(self):
        with self.assertRaises(ValueError):
            Meter(remote=f"http://127.0.0.1:{self.port}")

    def test_remote_balance_is_local_only(self):
        m = self._remote_meter()
        with self.assertRaises(PlutusError):
            m.balance()
        m.close()

    def test_remote_meter_sends_real_user_agent(self):
        # Cloudflare (error 1010) hard-blocks the default "Python-urllib" UA, so
        # the SDK must send its own or ingest breaks behind the proxy.
        import urllib.request as ur
        captured = {}

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return b'{"recorded": true, "cost_usd": 0.0, "balance_after": 0.0}'

        orig = ur.urlopen
        ur.urlopen = lambda req, *a, **k: (
            captured.update(ua=req.get_header("User-agent")) or _FakeResp())
        try:
            Meter(remote="http://x", api_key="plutus_sk_x").track(
                provider="anthropic", input_tokens=1)
        finally:
            ur.urlopen = orig
        self.assertTrue(captured["ua"])
        self.assertTrue(captured["ua"].startswith("plutus-agent"))
        self.assertNotIn("urllib", captured["ua"].lower())


class TestIngestQuota(unittest.TestCase):
    """Free org past its cap with hard-blocking on → 402."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Free Co", tier="free")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        # blow past the 10K free cap
        from plutus_agent import metering
        metering.record_usage(conn, cls.org_id, provider="anthropic",
                              input_tokens=11_000, cost_usd=0.0)
        conn.close()

        cfg = dict(DEFAULT_CONFIG)
        cfg["pricing"] = dict(cfg["pricing"], block_over_free_limit=True)
        ctx = app._Ctx(cfg, cls.dbpath, demo=False)
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

    def test_over_quota_returns_402(self):
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps({"provider": "anthropic", "input_tokens": 500}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 402")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 402)
            body = json.loads(e.read().decode())
            self.assertFalse(body["recorded"])
            self.assertIn("upgrade_url", body)

    def test_remote_meter_402_does_not_raise(self):
        # An over-quota event should report recorded=False, not crash the agent.
        m = Meter(remote=f"http://127.0.0.1:{self.port}", api_key=self.key)
        r = m.track(provider="anthropic", input_tokens=500)
        self.assertFalse(r.recorded)
        self.assertTrue(r.over_free_limit)
        m.close()


class TestBatchAtomicity(unittest.TestCase):
    """Fix #27: batch POST /v1/usage must be all-or-nothing."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Batch Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
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
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    
    def test_malformed_second_event_records_nothing(self):
        """Fix #27: if event 2 is invalid, event 1 must not commit."""
        conn = db.connect(self.dbpath)
        from plutus_agent import metering
        before = metering.tracked_tokens_mtd(conn, self.org_id)
        conn.close()
        
        status, body = self._post([
            {"provider": "anthropic", "input_tokens": 1000},
            {"provider": "", "input_tokens": 500},  # invalid: empty provider
        ])
        self.assertEqual(status, 400)
        
        conn = db.connect(self.dbpath)
        after = metering.tracked_tokens_mtd(conn, self.org_id)
        conn.close()
        self.assertEqual(before, after, "No tokens should have been recorded")


class TestPrepaidHardStop(unittest.TestCase):
    """Fix #28: block_over_balance prevents debits past zero."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Prepaid Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        db.add_ledger(conn, cls.org_id, 1.0, "topup")  # $1 credit
        conn.close()

        cfg = dict(DEFAULT_CONFIG)
        cfg["pricing"] = dict(cfg["pricing"], block_over_balance=True)
        ctx = app._Ctx(cfg, cls.dbpath, demo=False)
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
    
    def test_over_balance_returns_402(self):
        """Fix #28: POST /v1/usage with cost > balance returns 402."""
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps({"provider": "anthropic", "cost_usd": 10.0}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 402")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 402)
            body = json.loads(e.read().decode())
            self.assertFalse(body["recorded"])
            self.assertTrue(body["over_balance"])
            self.assertIn("credit exhausted", body["error"])


class TestBatchPartialBlock(unittest.TestCase):
    """#62: a batch with some over-balance rejections must surface them — a 200
    is not "all recorded", and the prepaid hard-stop count was missing entirely."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Prepaid Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        db.add_ledger(conn, cls.org_id, 1.0, "topup")  # $1 credit
        conn.close()
        cfg = dict(DEFAULT_CONFIG)
        cfg["pricing"] = dict(cfg["pricing"], block_over_balance=True)
        ctx = app._Ctx(cfg, cls.dbpath, demo=False)
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
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_partial_block_is_200_but_surfaces_over_balance(self):
        # First event lands ($0.50 of $1), second exceeds remaining credit.
        status, body = self._post([
            {"provider": "anthropic", "cost_usd": 0.50},
            {"provider": "anthropic", "cost_usd": 10.0},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["recorded"], 1)
        self.assertEqual(body["over_balance_blocked"], 1)
        self.assertEqual(body["free_limit_blocked"], 0)
        self.assertEqual(body["blocked"], 1)  # total includes the hard-stop

    def test_whole_batch_over_balance_returns_402(self):
        status, body = self._post([
            {"provider": "anthropic", "cost_usd": 10.0},
            {"provider": "anthropic", "cost_usd": 10.0},
        ])
        self.assertEqual(status, 402)
        self.assertEqual(body["recorded"], 0)
        self.assertEqual(body["over_balance_blocked"], 2)
        self.assertIn("credit exhausted", body["error"])


if __name__ == "__main__":
    unittest.main()


# ---- Security hardening tests (issues #31-#36) --------------------------------
class TestSecurityHardening(unittest.TestCase):
    """Tests for security fixes #31-#36."""
    
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=10)
        _, cls.key = db.create_api_key(conn, cls.org_id, name="test")
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

    def _post(self, path, payload, token=None):
        """Helper to POST JSON."""
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    # Fix #31: Body size limit
    def test_oversized_body_returns_413(self):
        """Oversized request body should return 413."""
        # Manually send a request with a huge Content-Length header
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("127.0.0.1", self.port))
            huge_size = 2 * 1024 * 1024  # 2 MiB
            request = (
                f"POST /v1/usage HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Authorization: Bearer {self.key}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {huge_size}\r\n"
                f"\r\n"
                # Send at least 1 byte to trigger the read, but server should reject before reading it all
                f"{{"
            )
            sock.sendall(request.encode())
            response = sock.recv(8192).decode()
            sock.close()
            
            self.assertIn("413", response)
            self.assertIn("too large", response.lower())
        finally:
            try:
                sock.close()
            except:
                pass

    def test_normal_body_size_works(self):
        """Normal-sized body should still work."""
        status, body = self._post("/v1/usage", {
            "provider": "anthropic",
            "input_tokens": 100
        }, token=self.key)
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])

    # Fix #34: HTML escaping in reports
    def test_report_escapes_xss_in_workspace_name(self):
        """Reports should escape workspace names containing HTML/script tags."""
        from plutus_agent import reports, db as db_mod
        conn = db_mod.connect(self.dbpath)
        
        # Create workspace with XSS payload
        xss_name = "<script>alert('xss')</script>"
        ws_id = db_mod.create_workspace(conn, self.org_id, xss_name)["id"]
        
        # Record usage to that workspace
        from plutus_agent import metering
        metering.record_usage(conn, self.org_id, provider="test", workspace=xss_name,
                            input_tokens=100, cost_usd=0.01)
        
        # Build and render report
        import datetime as dt
        now = dt.datetime.now()
        report = reports.build_report(conn, self.org_id, now.year, now.month)
        html = reports.render_html(report)
        
        conn.close()
        
        # The literal script tag should NOT appear in HTML
        self.assertNotIn("<script>", html)
        self.assertNotIn("</script>", html)
        # But the escaped version should
        self.assertIn("&lt;script&gt;", html)


class TestSameOrigin(unittest.TestCase):
    """Unit coverage for the same-origin check behind CSRF protection (Fix #32)."""

    def _check(self, base_url, headers):
        fake = types.SimpleNamespace()
        fake.ctx = types.SimpleNamespace(cfg={"auth": {"base_url": base_url}})
        fake.headers = headers
        return app.Handler._same_origin(fake)

    def test_matching_origin_allowed(self):
        self.assertTrue(self._check("https://app.example.com",
                                    {"Origin": "https://app.example.com"}))

    def test_mismatched_origin_blocked(self):
        self.assertFalse(self._check("https://app.example.com",
                                     {"Origin": "https://evil.example.com"}))

    def test_referer_fallback_allowed(self):
        self.assertTrue(self._check("https://app.example.com",
                                    {"Referer": "https://app.example.com/dashboard"}))

    def test_referer_fallback_blocked(self):
        self.assertFalse(self._check("https://app.example.com",
                                     {"Referer": "https://evil.example.com/x"}))

    def test_no_headers_blocked(self):
        # Absent Origin AND Referer is rejected for safety.
        self.assertFalse(self._check("https://app.example.com", {}))

    def test_unconfigured_base_url_falls_back_to_host(self):
        # Fix #32: with no base_url, judge origin against the request's own Host
        # header (fail closed) — not "allow anything", as the old code did.
        self.assertTrue(self._check(
            "", {"Host": "app.example.com", "Origin": "https://app.example.com"}))
        self.assertFalse(self._check(
            "", {"Host": "app.example.com", "Origin": "https://evil.example.com"}))

    def test_unconfigured_base_url_and_no_host_blocked(self):
        # Nothing to compare against → reject (previously this allowed through).
        self.assertFalse(self._check("", {"Origin": "https://anywhere.com"}))

    def test_origin_takes_precedence_over_referer(self):
        # A mismatched Origin blocks even when Referer would have matched.
        self.assertFalse(self._check(
            "https://app.example.com",
            {"Origin": "https://evil.example.com",
             "Referer": "https://app.example.com/x"}))

    def test_base_url_trailing_slash_normalized(self):
        self.assertTrue(self._check("https://app.example.com/",
                                    {"Origin": "https://app.example.com"}))


class TestBodyCap(unittest.TestCase):
    """Unit coverage for the request-body size limit (Fix #31)."""

    def _read(self, declared_len, payload, max_bytes):
        fake = types.SimpleNamespace()
        fake.headers = ({"Content-Length": str(declared_len)}
                        if declared_len is not None else {})
        fake.rfile = io.BytesIO(payload)
        return app.Handler._body(fake, max_bytes=max_bytes)

    def test_at_limit_allowed(self):
        self.assertEqual(self._read(10, b"x" * 10, max_bytes=10), b"x" * 10)

    def test_over_limit_raises(self):
        with self.assertRaises(app._BodyTooLarge):
            self._read(11, b"x" * 11, max_bytes=10)

    def test_missing_content_length_is_empty(self):
        self.assertEqual(self._read(None, b"", max_bytes=10), b"")

    def test_default_limit_is_one_mib(self):
        self.assertEqual(app.MAX_BODY_BYTES, 1 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
