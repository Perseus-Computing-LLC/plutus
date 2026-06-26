"""Google OIDC sign-in for the dashboard. Stdlib only — no web framework, no
auth library.

Authorization-code flow: we redirect the browser to Google, Google redirects
back to ``/auth/callback`` with a ``code``, and we exchange that code for tokens
at Google's token endpoint over TLS. Because the ``id_token`` arrives directly
from Google over a verified TLS channel, we read its claims after checking
``aud``/``iss``/``exp``/``nonce`` rather than re-verifying the RSA signature
ourselves. That keeps this dependency-free; swap in JWKS verification if you
later need to accept tokens from a less trusted path.

Access is allow-listed: an email may sign in only if it is already a member of
an org, or it matches ``auth.allowed_emails`` / ``auth.allowed_domain`` (in
which case it is provisioned into ``auth.provision_org_id``, or the sole org).

When ``auth.allow_signup`` is on, sign-in is **open**: any verified Google
account that isn't already known gets its *own* brand-new Free-tier org (the
self-serve SaaS path). The allow-list still takes precedence — an allow-listed
address joins the existing org as a member rather than spinning up a new one —
so "invite a teammate into my org" and "let strangers sign up" stay distinct.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from typing import Optional

from .. import db

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_ENDPOINT = "https://www.googleapis.com/oauth2/v3/certs"
SCOPES = "openid email profile"
COOKIE = "plutus_session"
_STATE_TTL = 600  # how long a login attempt's state/nonce stays valid (seconds)

# Self-serve signup rate limiting (per hour globally)
_SIGNUP_LIMIT = 5
_SIGNUP_WINDOW = 3600  # 1 hour in seconds

# state -> (nonce, expires_at). In-memory: a login starts and finishes on the
# same process, so this needs no persistence. Guarded for the threaded server.
_pending: dict[str, tuple[str, float]] = {}
_pending_lock = threading.Lock()

# Signup rate limiter: list of signup timestamps
_signup_times: list[float] = []
_signup_lock = threading.Lock()

# JWKS cache: {kid: (n, e, expires_at)}
_jwks_cache: dict[str, tuple[int, int, float]] = {}
_jwks_lock = threading.Lock()
_JWKS_TTL = 3600  # Cache JWKS for 1 hour


class AuthError(Exception):
    """Any failure in the sign-in flow; the caller renders a friendly page."""


# ----------------------------------------------------------------- helpers ---
def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _fetch_jwks() -> dict:
    """Fetch Google's JWKS (JSON Web Key Set)."""
    try:
        req = urllib.request.Request(JWKS_ENDPOINT, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        raise AuthError(f"failed to fetch JWKS: {e}")


def _get_public_key(kid: str) -> tuple[int, int]:
    """Get RSA public key (n, e) for the given key ID, with caching."""
    now = time.time()
    
    with _jwks_lock:
        # Check cache
        if kid in _jwks_cache:
            n, e, exp = _jwks_cache[kid]
            if exp > now:
                return n, e
        
        # Fetch fresh JWKS
        jwks = _fetch_jwks()
        keys = jwks.get("keys", [])
        
        # Update cache with all keys
        for key in keys:
            if key.get("kty") != "RSA":
                continue
            k_id = key.get("kid")
            if not k_id:
                continue
            
            # Decode n and e from base64url
            try:
                n_bytes = _b64url_decode(key["n"])
                e_bytes = _b64url_decode(key["e"])
                n_int = int.from_bytes(n_bytes, "big")
                e_int = int.from_bytes(e_bytes, "big")
                _jwks_cache[k_id] = (n_int, e_int, now + _JWKS_TTL)
            except Exception:
                continue
        
        # Return the requested key
        if kid in _jwks_cache:
            n, e, _ = _jwks_cache[kid]
            return n, e
        
        raise AuthError(f"key id '{kid}' not found in JWKS")


def _verify_rs256_signature(token: str) -> None:
    """Verify RS256 signature of a JWT token using Google's JWKS.
    
    Raises AuthError if signature verification fails.
    """
    import hashlib
    
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed JWT")
    
    header_b64, payload_b64, signature_b64 = parts
    
    # Decode header to get kid
    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception:
        raise AuthError("invalid JWT header")
    
    kid = header.get("kid")
    if not kid:
        raise AuthError("JWT header missing 'kid'")
    
    alg = header.get("alg")
    if alg != "RS256":
        raise AuthError(f"unsupported algorithm: {alg}")
    
    # Get public key
    n, e = _get_public_key(kid)
    
    # Decode signature
    try:
        signature = _b64url_decode(signature_b64)
    except Exception:
        raise AuthError("invalid signature encoding")
    
    # Convert signature to integer
    sig_int = int.from_bytes(signature, "big")
    
    # RSA verification: decrypt signature with public key
    decrypted_int = pow(sig_int, e, n)
    
    # Convert to bytes (same length as modulus)
    n_bytes_len = (n.bit_length() + 7) // 8
    try:
        decrypted = decrypted_int.to_bytes(n_bytes_len, "big")
    except Exception:
        raise AuthError("signature verification failed: invalid padding")
    
    # EMSA-PKCS1-v1_5 structure for SHA-256:
    # 0x00 0x01 0xFF...0xFF 0x00 || DigestInfo || Hash
    # DigestInfo for SHA-256: 30 31 30 0d 06 09 60 86 48 01 65 03 04 02 01 05 00 04 20
    digestinfo_sha256 = bytes.fromhex("3031300d060960864801650304020105000420")
    
    # Compute expected hash
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_hash = hashlib.sha256(signing_input).digest()
    
    # Expected decrypted structure
    expected_suffix = digestinfo_sha256 + expected_hash
    expected_suffix_len = len(expected_suffix)
    
    # Check structure: 0x00 0x01 0xFF+ 0x00 digestinfo hash
    if len(decrypted) < expected_suffix_len + 11:
        raise AuthError("signature verification failed: message too short")
    
    if decrypted[0] != 0x00 or decrypted[1] != 0x01:
        raise AuthError("signature verification failed: bad header")
    
    # Find the 0x00 separator
    separator_idx = None
    for i in range(2, len(decrypted) - expected_suffix_len):
        if decrypted[i] == 0x00:
            separator_idx = i
            break
        if decrypted[i] != 0xFF:
            raise AuthError("signature verification failed: bad padding")
    
    if separator_idx is None or separator_idx < 10:
        raise AuthError("signature verification failed: separator not found")
    
    # Check the digestinfo + hash
    actual_suffix = decrypted[separator_idx + 1:]
    if actual_suffix != expected_suffix:
        raise AuthError("signature verification failed: hash mismatch")


def _redirect_uri(cfg) -> str:
    base = (cfg.get("auth", {}).get("base_url") or "").rstrip("/")
    if not base:
        raise AuthError("auth.base_url is not set (need the public origin)")
    return f"{base}/auth/callback"


def _remember_state(state: str, nonce: str) -> None:
    now = time.time()
    with _pending_lock:
        for k in [k for k, (_, exp) in _pending.items() if exp <= now]:
            _pending.pop(k, None)
        _pending[state] = (nonce, now + _STATE_TTL)


def _take_state(state: str) -> Optional[str]:
    with _pending_lock:
        item = _pending.pop(state, None)
    if not item:
        return None
    nonce, exp = item
    return nonce if exp > time.time() else None


# ---------------------------------------------------------------- the flow ---
def login_url(cfg) -> str:
    """Build the Google authorization URL and stash its state/nonce."""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    _remember_state(state, nonce)
    params = {
        "client_id": cfg["auth"]["google_client_id"],
        "redirect_uri": _redirect_uri(cfg),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "access_type": "online",
        "prompt": "select_account",
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _exchange_code(cfg, code: str) -> dict:
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": cfg["auth"]["google_client_id"],
        "client_secret": cfg["auth"]["google_client_secret"],
        "redirect_uri": _redirect_uri(cfg),
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise AuthError(f"token exchange failed: {e.code} {e.read().decode()[:200]}")
    except Exception as e:  # network etc.
        raise AuthError(f"token exchange error: {e}")


def _claims_from_id_token(id_token: str, cfg, nonce: str) -> dict:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed id_token")
    # Verify the RS256 signature against Google's JWKS. Skipping it is gated
    # behind an explicit, default-off config flag (``auth.allow_unsigned_tokens``)
    # used only by the test suite to inject fake tokens — previously any token
    # whose header segment literally equalled "hdr" silently bypassed the
    # verifier on the production path (#37).
    if not cfg.get("auth", {}).get("allow_unsigned_tokens"):
        _verify_rs256_signature(id_token)
    claims = json.loads(_b64url_decode(parts[1]))
    if claims.get("aud") != cfg["auth"]["google_client_id"]:
        raise AuthError("id_token audience mismatch")
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise AuthError("id_token issuer mismatch")
    if float(claims.get("exp", 0)) <= time.time():
        raise AuthError("id_token expired")
    if nonce and claims.get("nonce") != nonce:
        raise AuthError("id_token nonce mismatch")
    if not claims.get("email"):
        raise AuthError("id_token has no email")
    if claims.get("email_verified") not in (True, "true"):
        raise AuthError("email is not verified")
    return claims


def _only_org(conn) -> Optional[str]:
    orgs = db.list_orgs(conn)
    return orgs[0]["id"] if len(orgs) == 1 else None


def _org_name_for(email: str, name: Optional[str]) -> str:
    """A friendly default org name for a self-serve signup."""
    base = (name or "").strip() or email.split("@", 1)[0]
    return f"{base}'s workspace"


def _allow_signup_now() -> bool:
    """Check if a new self-serve signup is allowed (rate limiting)."""
    now = time.time()
    with _signup_lock:
        # Prune old timestamps
        cutoff = now - _SIGNUP_WINDOW
        while _signup_times and _signup_times[0] < cutoff:
            _signup_times.pop(0)
        
        if len(_signup_times) >= _SIGNUP_LIMIT:
            return False
        
        # Record this signup
        _signup_times.append(now)
        return True


def _authorize_email(conn, cfg, email: str, name: Optional[str] = None) -> Optional[str]:
    """Return the user_id to bind a session to, or None if not allowed.

    Existing members sign in as themselves. A newly-allowed email (via
    allowed_emails / allowed_domain) is provisioned as a 'member' of
    provision_org_id, or of the sole org if there is exactly one. When
    ``allow_signup`` is set, any other verified email gets its own new
    Free-tier org as 'owner'.
    """
    auth = cfg.get("auth", {})
    members = db.users_by_email(conn, email)
    if members:
        return members[0]["id"]

    allowed = email.lower() in [e.lower() for e in auth.get("allowed_emails", [])]
    dom = (auth.get("allowed_domain") or "").lower().lstrip("@")
    if dom and email.lower().endswith("@" + dom):
        allowed = True
    if allowed:
        org_id = auth.get("provision_org_id") or _only_org(conn)
        if org_id and db.get_org(conn, org_id):
            return db.ensure_user(conn, org_id, email, name=name, role="member")["id"]
        # allow-listed but no org to join → fall through to self-serve if open

    if auth.get("allow_signup"):
        # Rate limit self-serve signups
        if not _allow_signup_now():
            raise AuthError("signup temporarily rate-limited, try again later")
        
        org = db.create_org(conn, _org_name_for(email, name), tier="free",
                            owner_email=email, owner_name=name)
        return db.users_by_email(conn, email)[0]["id"]

    return None


def handle_callback(conn, cfg, q: dict) -> str:
    """Process /auth/callback query params; return a fresh session token.

    Raises :class:`AuthError` on any problem.
    """
    if q.get("error"):
        raise AuthError(f"Google returned an error: {q['error']}")
    code = q.get("code")
    state = q.get("state")
    if not code or not state:
        raise AuthError("missing code or state")
    nonce = _take_state(state)
    if nonce is None:
        raise AuthError("invalid or expired sign-in state — try again")

    tokens = _exchange_code(cfg, code)
    id_token = tokens.get("id_token")
    if not id_token:
        raise AuthError("no id_token in Google's token response")
    claims = _claims_from_id_token(id_token, cfg, nonce)

    email = claims["email"]
    user_id = _authorize_email(conn, cfg, email, name=claims.get("name"))
    if not user_id:
        raise AuthError(f"{email} is not authorized to use this Plutus instance")

    ttl = float(cfg["auth"].get("session_ttl_hours", 168)) * 3600
    return db.create_session(conn, user_id, ttl)


# ----------------------------------------------------------- cookie helpers ---
def read_cookie(handler) -> str:
    raw = handler.headers.get("Cookie", "")
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return ""
    m = jar.get(COOKIE)
    return m.value if m else ""


def _secure_attr(cfg) -> str:
    base = (cfg.get("auth", {}).get("base_url") or "").lower()
    return "; Secure" if base.startswith("https") else ""


def set_cookie_header(token: str, cfg, *, max_age: Optional[int] = None) -> str:
    if max_age is None:
        max_age = int(float(cfg["auth"].get("session_ttl_hours", 168)) * 3600)
    return (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
            f"{_secure_attr(cfg)}; Max-Age={max_age}")


def clear_cookie_header(cfg) -> str:
    return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax{_secure_attr(cfg)}; Max-Age=0"


def current_user(handler, conn):
    """The signed-in user row for this request, or None."""
    return db.session_user(conn, read_cookie(handler))
