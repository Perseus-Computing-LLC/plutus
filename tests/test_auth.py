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
        # Tests inject unsigned ``_jwt`` tokens; opt into the (default-off)
        # signature-skip flag. Tests that exercise the real RS256 verifier set
        # this back to False explicitly.
        "allow_unsigned_tokens": True,
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


# A fixed RSA-2048 test keypair, generated once offline. The tests below sign
# tokens with the private exponent in pure Python (no crypto deps, no network)
# and patch _get_public_key to return its public half — so RS256 verification is
# exercised deterministically instead of fetching Google's live JWKS.
_RSA_N = 24082071316944105535264363308125315081250696206379611006902791111414462803792342111092086488077527810965707259282176039699553715360063115605638203558350292866326474214097340832177899311999630061207584261420827254645947602202512245817930496082738620746378204581118753661611277201038248541169972056179138472964709176368076222696056548081502168410035626513049142471362210716033646277565923118257369951859210616352517546435402478729200315896344136226664836868186780117004177629252579457368833344326120719796058132245097292213156637867134965004036885984902882614855313331086730291643959970615985956228741256116766962875983
_RSA_E = 65537
_RSA_D = 5981107997404507465973389712168023461213018327833756936682434821863881640254023720070279258532400326214028976903672420131980954650285294302653051548274527625390176858612118600567002870156064185212564643226678139430735143827918455607953593920056403709184095029782717447396840853278294268956827977454382057836665026945284258000196374479498734116942844985015516975701280188053120785912950111729410279298376217763849433774214947138601296886781903609166930783353977143208142704433270702111739128147392561960129054716546251593262583189855898776171535583215414011419089715420530570962946095746158781756129468041066764401161


def _b64url_str(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign_rs256(header: dict, claims: dict) -> str:
    """Produce a JWT signed with the fixed test key (EMSA-PKCS1-v1_5, SHA-256)."""
    import hashlib

    h_b64 = _b64url_str(json.dumps(header).encode())
    p_b64 = _b64url_str(json.dumps(claims).encode())
    digest = hashlib.sha256(f"{h_b64}.{p_b64}".encode("ascii")).digest()
    digestinfo = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    k = (_RSA_N.bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(digestinfo) - 3) + b"\x00" + digestinfo
    sig = pow(int.from_bytes(em, "big"), _RSA_D, _RSA_N)
    return f"{h_b64}.{p_b64}.{_b64url_str(sig.to_bytes(k, 'big'))}"


class TestRS256Verification(unittest.TestCase):
    """Deterministic RS256 signature verification, no network (Fix #36)."""

    def setUp(self):
        self._orig_get_key = authmod._get_public_key
        authmod._get_public_key = lambda kid: (_RSA_N, _RSA_E)

    def tearDown(self):
        authmod._get_public_key = self._orig_get_key

    def test_valid_signature_accepted(self):
        tok = _sign_rs256({"kid": "test", "alg": "RS256"}, {"sub": "x"})
        authmod._verify_rs256_signature(tok)  # must not raise

    def test_tampered_payload_rejected(self):
        tok = _sign_rs256({"kid": "test", "alg": "RS256"}, {"email": "real@example.com"})
        h, _p, s = tok.split(".")
        forged = f"{h}.{_b64url_str(json.dumps({'email': 'attacker@evil.com'}).encode())}.{s}"
        with self.assertRaises(authmod.AuthError):
            authmod._verify_rs256_signature(forged)

    def test_alg_none_downgrade_rejected(self):
        # An attacker who flips alg to "none" must not bypass verification.
        tok = _sign_rs256({"kid": "test", "alg": "none"}, {"sub": "x"})
        with self.assertRaises(authmod.AuthError) as cm:
            authmod._verify_rs256_signature(tok)
        self.assertIn("algorithm", str(cm.exception).lower())

    def test_alg_hs256_rejected(self):
        tok = _sign_rs256({"kid": "test", "alg": "HS256"}, {"sub": "x"})
        with self.assertRaises(authmod.AuthError):
            authmod._verify_rs256_signature(tok)

    def test_missing_kid_rejected(self):
        tok = _sign_rs256({"alg": "RS256"}, {"sub": "x"})
        with self.assertRaises(authmod.AuthError) as cm:
            authmod._verify_rs256_signature(tok)
        self.assertIn("kid", str(cm.exception).lower())

    def test_malformed_token_rejected(self):
        with self.assertRaises(authmod.AuthError):
            authmod._verify_rs256_signature("only.two")

    def test_wrong_key_rejected(self):
        tok = _sign_rs256({"kid": "test", "alg": "RS256"}, {"sub": "x"})
        authmod._get_public_key = lambda kid: (_RSA_N - 2, _RSA_E)  # different modulus
        with self.assertRaises(authmod.AuthError):
            authmod._verify_rs256_signature(tok)

    def test_claims_path_runs_verification(self):
        # With allow_unsigned_tokens off, _claims_from_id_token runs the real verifier.
        cfg = _auth_cfg(allow_unsigned_tokens=False)
        good = {"aud": cfg["auth"]["google_client_id"],
                "iss": "https://accounts.google.com", "exp": time.time() + 3600,
                "nonce": "n", "email": "owner@example.com", "email_verified": True}
        tok = _sign_rs256({"kid": "test", "alg": "RS256"}, good)
        self.assertEqual(
            authmod._claims_from_id_token(tok, cfg, "n")["email"], "owner@example.com")
        # Same path rejects a token whose payload was swapped after signing.
        h, _p, s = tok.split(".")
        bad = f"{h}.{_b64url_str(json.dumps({**good, 'email': 'evil@x.com'}).encode())}.{s}"
        with self.assertRaises(authmod.AuthError):
            authmod._claims_from_id_token(bad, cfg, "n")


class TestCSRFEnforcement(unittest.TestCase):
    """Cookie-authenticated state-changing POSTs must be same-origin (Fix #32)."""

    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=10)
        cls.user = db.ensure_user(conn, cls.org_id, "owner@example.com",
                                  name="Owner", role="owner")
        cls.token = db.create_session(conn, cls.user["id"], ttl_seconds=3600)
        _, cls.api_key = db.create_api_key(conn, cls.org_id, name="csrf-test")
        conn.close()

        ctx = app._Ctx(_auth_cfg(), cls.dbpath, demo=True)
        assert ctx.auth_on, "auth must be on for CSRF enforcement"
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        # base_url must include the bound ephemeral port so same-origin can match.
        ctx.cfg["auth"]["base_url"] = f"http://127.0.0.1:{cls.port}"
        cls.origin = f"http://127.0.0.1:{cls.port}"
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

    def _post(self, path, cookie=None, origin=None, bearer=None, data=b""):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {}
        if cookie:
            headers["Cookie"] = f"plutus_session={cookie}"
        if origin:
            headers["Origin"] = origin
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_cookie_post_without_origin_blocked(self):
        status, body = self._post("/auth/logout", cookie=self.token)
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", body)

    def test_cookie_post_cross_origin_blocked(self):
        status, body = self._post("/auth/logout", cookie=self.token,
                                  origin="http://evil.example.com")
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", body)

    def test_cookie_post_same_origin_allowed(self):
        # Same-origin cookie POST clears the CSRF gate (reaches the handler).
        status, body = self._post("/keys/create", cookie=self.token,
                                  origin=self.origin,
                                  data=f"org={self.org_id}".encode())
        self.assertNotIn("cross-origin", body)

    def test_bearer_usage_exempt_from_csrf(self):
        # Bearer-authenticated /v1/usage is exempt — cross-origin must NOT block it.
        status, body = self._post("/v1/usage", bearer=self.api_key,
                                  origin="http://evil.example.com", data=b"{}")
        self.assertNotEqual(status, 403)
        self.assertNotIn("cross-origin", body)


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

        cfg = _auth_cfg(allow_unsigned_tokens=False)  # exercise the real verifier

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
