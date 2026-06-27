#!/usr/bin/env python3
"""#66: coverage for the highest-risk auth/tenant paths that were untested:

- the hand-rolled OIDC RS256 verifier (every other auth test sets
  ``allow_unsigned_tokens``, so the signature math itself was never exercised);
- the ``_authz_org`` cross-tenant ``PermissionError`` path;
- the ``allow_negative_balance`` exemption end-to-end over HTTP.
"""
import base64
import hashlib
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app, auth


# --------------------------------------------------------------- RSA helpers ---
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _is_prime(n: int, rounds: int = 24) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2; r += 1
    for _ in range(rounds):
        a = random.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        cand = random.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(cand):
            return cand


def _make_rsa_key():
    e = 65537
    while True:
        p, q = _gen_prime(512), _gen_prime(512)
        if p == q:
            continue
        n, phi = p * q, (p - 1) * (q - 1)
        if math.gcd(e, phi) == 1:
            return n, e, pow(e, -1, phi)


def _sign_rs256(n: int, d: int, signing_input: bytes) -> bytes:
    digestinfo = (bytes.fromhex("3031300d060960864801650304020105000420")
                  + hashlib.sha256(signing_input).digest())
    n_len = (n.bit_length() + 7) // 8
    pad = b"\xff" * (n_len - len(digestinfo) - 3)
    block = b"\x00\x01" + pad + b"\x00" + digestinfo
    s = pow(int.from_bytes(block, "big"), d, n)
    return s.to_bytes(n_len, "big")


def _make_token(n, e, d, kid="test-kid", payload=None):
    header = _b64url(json.dumps({"alg": "RS256", "kid": kid}).encode())
    body = _b64url(json.dumps(payload or {"sub": "u1"}).encode())
    sig = _b64url(_sign_rs256(n, d, f"{header}.{body}".encode("ascii")))
    return f"{header}.{body}.{sig}"


class TestRs256Verifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.n, cls.e, cls.d = _make_rsa_key()
        cls.kid = "test-kid"
        with auth._jwks_lock:
            auth._jwks_cache[cls.kid] = (cls.n, cls.e, time.time() + 3600)

    @classmethod
    def tearDownClass(cls):
        with auth._jwks_lock:
            auth._jwks_cache.pop(cls.kid, None)

    def test_valid_signature_passes(self):
        tok = _make_token(self.n, self.e, self.d, self.kid)
        auth._verify_rs256_signature(tok)  # must not raise

    def test_tampered_payload_rejected(self):
        tok = _make_token(self.n, self.e, self.d, self.kid)
        header, _, sig = tok.split(".")
        forged = _b64url(json.dumps({"sub": "attacker"}).encode())
        with self.assertRaises(auth.AuthError):
            auth._verify_rs256_signature(f"{header}.{forged}.{sig}")

    def test_non_rs256_alg_rejected(self):
        header = _b64url(json.dumps({"alg": "none", "kid": self.kid}).encode())
        body = _b64url(json.dumps({"sub": "u1"}).encode())
        with self.assertRaises(auth.AuthError):
            auth._verify_rs256_signature(f"{header}.{body}.")


class TestAuthzCrossTenant(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.orgA = db.create_org(self.conn, "A", owner_email="a@x.com")["id"]
        self.orgB = db.create_org(self.conn, "B", owner_email="b@x.com")["id"]

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def _handler_for(self, email):
        # _authz_org only reads self._user, so a stub is sufficient.
        h = types.SimpleNamespace(_user={"email": email})
        return h

    def test_member_can_request_own_org(self):
        h = self._handler_for("a@x.com")
        self.assertEqual(
            app.Handler._authz_org(h, self.conn, self.orgA), self.orgA)

    def test_cross_tenant_request_raises_permission_error(self):
        h = self._handler_for("a@x.com")
        with self.assertRaises(PermissionError):
            app.Handler._authz_org(h, self.conn, self.orgB)

    def test_anonymous_falls_back_to_default(self):
        h = types.SimpleNamespace(_user=None)
        # No requested org → default to the first org (lenient, unauthenticated).
        self.assertIn(app.Handler._authz_org(h, self.conn, None),
                      (self.orgA, self.orgB))


class TestAllowNegativeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org = db.create_org(conn, "Internal", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org)
        db.add_ledger(conn, cls.org, 1.0, "topup")          # $1 credit
        db.set_org_allow_negative(conn, cls.org, True)       # exempt from hard-stop
        conn.close()
        cfg = dict(DEFAULT_CONFIG)
        cfg["pricing"] = dict(cfg["pricing"], block_over_balance=True)
        ctx = app._Ctx(cfg, cls.dbpath, demo=False)
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

    def test_exempt_org_records_into_negative_over_http(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/usage",
            data=json.dumps({"provider": "anthropic", "cost_usd": 10.0}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        r = urllib.request.urlopen(req, timeout=5)
        self.assertEqual(r.status, 200)
        body = json.loads(r.read().decode())
        self.assertTrue(body["recorded"])
        conn = db.connect(self.dbpath)
        try:
            self.assertLess(db.get_balance(conn, self.org), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
