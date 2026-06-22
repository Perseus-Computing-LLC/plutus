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
SCOPES = "openid email profile"
COOKIE = "plutus_session"
_STATE_TTL = 600  # how long a login attempt's state/nonce stays valid (seconds)

# state -> (nonce, expires_at). In-memory: a login starts and finishes on the
# same process, so this needs no persistence. Guarded for the threaded server.
_pending: dict[str, tuple[str, float]] = {}
_pending_lock = threading.Lock()


class AuthError(Exception):
    """Any failure in the sign-in flow; the caller renders a friendly page."""


# ----------------------------------------------------------------- helpers ---
def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


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
    if claims.get("email_verified") in (False, "false"):
        raise AuthError("email is not verified")
    return claims


def _only_org(conn) -> Optional[str]:
    orgs = db.list_orgs(conn)
    return orgs[0]["id"] if len(orgs) == 1 else None


def _authorize_email(conn, cfg, email: str, name: Optional[str] = None) -> Optional[str]:
    """Return the user_id to bind a session to, or None if not allowed.

    Existing members sign in as themselves. A newly-allowed email (via
    allowed_emails / allowed_domain) is provisioned as a 'member' of
    provision_org_id, or of the sole org if there is exactly one.
    """
    auth = cfg.get("auth", {})
    members = db.users_by_email(conn, email)
    if members:
        return members[0]["id"]

    allowed = email.lower() in [e.lower() for e in auth.get("allowed_emails", [])]
    dom = (auth.get("allowed_domain") or "").lower().lstrip("@")
    if dom and email.lower().endswith("@" + dom):
        allowed = True
    if not allowed:
        return None

    org_id = auth.get("provision_org_id") or _only_org(conn)
    if not org_id or not db.get_org(conn, org_id):
        return None
    return db.ensure_user(conn, org_id, email, name=name, role="member")["id"]


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
