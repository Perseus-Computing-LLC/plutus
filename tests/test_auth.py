#!/usr/bin/env python3
"""Auth: sessions, allow-listing, OIDC callback, and live server enforcement."""
import base64
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import config as cfgmod, db, demo
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app, auth as authmod


def _auth_cfg(**over):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["auth"].update({
        "enabled": True,
        "google_client_id": "cid.apps.googleusercontent.com",
        "google_client_secret": "secret",
        "base_url": "http://127.0.0.1",
    })
    cfg["auth"].update(over)
    return cfg


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"hdr.{payload}.sig"


class TestSessionsAndAllowlist(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Acme", owner_email="owner@example.com")
        self.user = db.users_by_email(self.conn, "owner@example.com")[0]

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_session_lifecycle(self):
        tok = db.create_session(self.conn, self.user["id"], ttl_seconds=60)
        self.assertEqual(db.session_user(self.conn, tok)["email"], "owner@example.com")
        db.delete_session(self.conn, tok)
        self.assertIsNone(db.session_user(self.conn, tok))

    def test_expired_session_rejected(self):
        tok = db.create_session(self.conn, self.user["id"], ttl_seconds=-1)
        self.assertIsNone(db.session_user(self.conn, tok))
        self.assertEqual(db.purge_expired_sessions(self.conn), 1)

    def test_member_authorized(self):
        uid = authmod._authorize_email(self.conn, _auth_cfg(), "owner@example.com")
        self.assertEqual(uid, self.user["id"])

    def test_email_case_insensitive(self):
        uid = authmod._authorize_email(self.conn, _auth_cfg(), "OWNER@Example.com")
        self.assertEqual(uid, self.user["id"])

    def test_stranger_denied(self):
        self.assertIsNone(
            authmod._authorize_email(self.conn, _auth_cfg(), "nobody@example.com"))

    def test_allowed_email_provisioned(self):
        cfg = _auth_cfg(allowed_emails=["new@example.com"],
                        provision_org_id=self.org["id"])
        uid = authmod._authorize_email(self.conn, cfg, "new@example.com", name="New")
        self.assertIsNotNone(uid)
        self.assertTrue(db.email_in_org(self.conn, "new@example.com", self.org["id"]))

    def test_allowed_domain_provisioned_into_sole_org(self):
        cfg = _auth_cfg(allowed_domain="example.com")  # sole org → auto target
        uid = authmod._authorize_email(self.conn, cfg, "dept@example.com")
        self.assertIsNotNone(uid)

    def test_orgs_scoped_to_membership(self):
        other = db.create_org(self.conn, "Other", owner_email="them@other.com")
        mine = db.list_orgs_for_email(self.conn, "owner@example.com")
        self.assertEqual([o["id"] for o in mine], [self.org["id"]])
        self.assertFalse(db.email_in_org(self.conn, "owner@example.com", other["id"]))

    # --- self-serve open signup -------------------------------------------
    def test_signup_off_denies_stranger(self):
        cfg = _auth_cfg(allow_signup=False)
        self.assertIsNone(
            authmod._authorize_email(self.conn, cfg, "fresh@stranger.com"))

    def test_signup_on_provisions_own_org(self):
        before = {o["id"] for o in db.list_orgs(self.conn)}
        cfg = _auth_cfg(allow_signup=True)
        uid = authmod._authorize_email(self.conn, cfg, "fresh@stranger.com",
                                       name="Fresh Dev")
        self.assertIsNotNone(uid)
        user = db.get_user(self.conn, uid)
        self.assertEqual(user["role"], "owner")
        self.assertEqual(user["name"], "Fresh Dev")
        # a brand-new org was created for them (not Acme)
        new_orgs = [o for o in db.list_orgs(self.conn) if o["id"] not in before]
        self.assertEqual(len(new_orgs), 1)
        self.assertEqual(new_orgs[0]["id"], user["org_id"])
        self.assertEqual(new_orgs[0]["tier"], "free")
        self.assertNotEqual(new_orgs[0]["id"], self.org["id"])

    def test_signup_allowlist_takes_precedence_over_open(self):
        # an allow-listed email joins the existing org as a member, even with
        # open signup on — it does NOT spin up a separate org.
        cfg = _auth_cfg(allow_signup=True, allowed_emails=["teammate@example.com"],
                        provision_org_id=self.org["id"])
        n_before = len(db.list_orgs(self.conn))
        uid = authmod._authorize_email(self.conn, cfg, "teammate@example.com")
        self.assertTrue(db.email_in_org(self.conn, "teammate@example.com", self.org["id"]))
        self.assertEqual(db.get_user(self.conn, uid)["role"], "member")
        self.assertEqual(len(db.list_orgs(self.conn)), n_before)

    def test_signup_existing_member_signs_in_as_self(self):
        cfg = _auth_cfg(allow_signup=True)
        uid = authmod._authorize_email(self.conn, cfg, "owner@example.com")
        self.assertEqual(uid, self.user["id"])


class TestOIDCCallback(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        db.create_org(self.conn, "Acme", owner_email="owner@example.com")
        self.cfg = _auth_cfg()

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def _good_claims(self, nonce, **over):
        c = {"aud": self.cfg["auth"]["google_client_id"],
             "iss": "https://accounts.google.com",
             "exp": time.time() + 3600, "nonce": nonce,
             "email": "owner@example.com", "email_verified": True, "name": "Owner"}
        c.update(over)
        return c

    def test_claims_validation_rejects_bad_token(self):
        nonce = "n1"
        bad = [
            self._good_claims(nonce, aud="someone-else"),
            self._good_claims(nonce, iss="https://evil.example"),
            self._good_claims(nonce, exp=time.time() - 1),
            self._good_claims("other-nonce"),
            self._good_claims(nonce, email_verified=False),
        ]
        for claims in bad:
            with self.assertRaises(authmod.AuthError):
                authmod._claims_from_id_token(_jwt(claims), self.cfg, nonce)

    def test_callback_creates_session(self):
        url = authmod.login_url(self.cfg)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state, nonce = qs["state"][0], qs["nonce"][0]

        orig = authmod._exchange_code
        authmod._exchange_code = lambda cfg, code: {"id_token": _jwt(self._good_claims(nonce))}
        try:
            token = authmod.handle_callback(
                self.conn, self.cfg, {"code": "abc", "state": state})
        finally:
            authmod._exchange_code = orig

        self.assertEqual(db.session_user(self.conn, token)["email"], "owner@example.com")

    def test_callback_rejects_unknown_state(self):
        with self.assertRaises(authmod.AuthError):
            authmod.handle_callback(self.conn, self.cfg,
                                    {"code": "abc", "state": "never-issued"})

    def test_callback_open_signup_creates_session_for_stranger(self):
        cfg = _auth_cfg(allow_signup=True)
        url = authmod.login_url(cfg)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state, nonce = qs["state"][0], qs["nonce"][0]
        claims = self._good_claims(nonce, email="newbie@elsewhere.com", name="Newbie")
        orig = authmod._exchange_code
        authmod._exchange_code = lambda c, code: {"id_token": _jwt(claims)}
        try:
            token = authmod.handle_callback(self.conn, cfg,
                                            {"code": "abc", "state": state})
        finally:
            authmod._exchange_code = orig
        self.assertEqual(db.session_user(self.conn, token)["email"], "newbie@elsewhere.com")


class TestServerEnforcement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=40)
        cls.user = db.ensure_user(conn, cls.org_id, "owner@example.com",
                                  name="Owner", role="owner")
        cls.other_org = db.create_org(conn, "Outsider", owner_email="x@x.com")["id"]
        cls.token = db.create_session(conn, cls.user["id"], ttl_seconds=3600)
        conn.close()

        ctx = app._Ctx(_auth_cfg(), cls.dbpath, demo=True)
        assert ctx.auth_on, "auth should be enabled for this test"
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

    def _req(self, path, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Cookie": f"plutus_session={cookie}"} if cookie else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()

    def test_healthz_public(self):
        status, _ = self._req("/healthz")
        self.assertEqual(status, 200)

    def test_unauthenticated_lands_on_login(self):
        # GET / → 303 to /auth/login (followed) → login page, NOT the dashboard
        status, body = self._req("/")
        self.assertEqual(status, 200)
        self.assertIn("Sign in with Google", body)
        self.assertNotIn("Credit balance", body)

    def test_api_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req("/api/summary")
        self.assertEqual(cm.exception.code, 401)

    def test_authenticated_dashboard(self):
        status, body = self._req("/", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn("Credit balance", body)
        self.assertIn("Sign out", body)

    def test_cross_org_forbidden(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(f"/api/summary?org={self.other_org}", cookie=self.token)
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()


# ---- Security hardening tests (issues #33, #36) --------------------------------
class TestSignupRateLimit(unittest.TestCase):
    """Test signup rate limiting (Fix #33)."""
    
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        # Clear rate limiter state
        authmod._signup_times.clear()
    
    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass
    
    def test_signup_rate_limit_trips(self):
        """After N signups, the rate limiter should block new signups."""
        cfg = _auth_cfg(allow_signup=True)
        
        # Do 5 signups (the limit)
        for i in range(5):
            uid = authmod._authorize_email(self.conn, cfg, f"user{i}@example.com")
            self.assertIsNotNone(uid)
        
        # The 6th should be rate-limited
        with self.assertRaises(authmod.AuthError) as cm:
            authmod._authorize_email(self.conn, cfg, "blocked@example.com")
        self.assertIn("rate-limited", str(cm.exception))
    
    def test_existing_member_not_rate_limited(self):
        """Existing members should bypass rate limiting."""
        cfg = _auth_cfg(allow_signup=True)
        
        # Create existing member
        org = db.create_org(self.conn, "Existing", owner_email="member@example.com")
        
        # Exhaust the rate limit
        for i in range(5):
            authmod._authorize_email(self.conn, cfg, f"new{i}@example.com")
        
        # Existing member should still work
        uid = authmod._authorize_email(self.conn, cfg, "member@example.com")
        self.assertIsNotNone(uid)


class TestOIDCSignatureVerification(unittest.TestCase):
    """Test OIDC id_token signature verification (Fix #36)."""
    
    def test_tampered_signature_rejected(self):
        """A token with a tampered signature should raise AuthError."""
        # Create a fake token with invalid signature
        header = base64.urlsafe_b64encode(
            json.dumps({"kid": "fake", "alg": "RS256"}).encode()
        ).rstrip(b"=").decode()
        
        claims = {
            "aud": "test.apps.googleusercontent.com",
            "iss": "accounts.google.com",
            "exp": time.time() + 3600,
            "nonce": "test-nonce",
            "email": "test@example.com",
            "email_verified": True
        }
        payload = base64.urlsafe_b64encode(
            json.dumps(claims).encode()
        ).rstrip(b"=").decode()
        
        # Bogus signature
        signature = base64.urlsafe_b64encode(b"bogus_signature").rstrip(b"=").decode()
        fake_token = f"{header}.{payload}.{signature}"
        
        cfg = _auth_cfg()
        
        # Should raise AuthError during signature verification
        with self.assertRaises(authmod.AuthError) as cm:
            authmod._claims_from_id_token(fake_token, cfg, "test-nonce")
        
        # The error should be about signature verification or JWKS
        error_msg = str(cm.exception).lower()
        self.assertTrue(
            "signature" in error_msg or "jwks" in error_msg or "key" in error_msg,
            f"Expected signature/JWKS error, got: {cm.exception}"
        )


if __name__ == "__main__":
    unittest.main()
