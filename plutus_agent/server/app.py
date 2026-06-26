"""HTTP server — stdlib ``http.server`` only. No web framework dependency.

Serves the dark dashboard, a JSON API, Stripe Checkout/Portal redirects, and the
Stripe webhook, all at ``:8420`` by default. Threaded, with a fresh SQLite
connection per request (SQLite connections aren't thread-safe to share).
"""
from __future__ import annotations

import html
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .. import __version__, bridge, config as cfgmod, db
from ..billing import StripeClient, BillingError, handle_webhook_event
from ..utils import strict_int
from . import api, views, auth as authmod

# Paths reachable without a session when auth is enabled.
_PUBLIC_PATHS = {"/healthz", "/favicon.ico", "/webhook/stripe", "/pricing",
                 "/v1/usage",  # authenticated by its own Bearer API key, not a session
                 "/auth/login", "/auth/callback", "/auth/logout"}

# Max request body size (1 MiB) — protects /v1/usage and /webhook/stripe from DoS
MAX_BODY_BYTES = 1 * 1024 * 1024


class _BodyTooLarge(Exception):
    """Raised when Content-Length exceeds MAX_BODY_BYTES."""


class _Ctx:
    """Shared, read-mostly server context."""
    def __init__(self, cfg, db_path, demo=False):
        self.cfg = cfg
        self.db_path = db_path
        self.demo = demo
        self.stripe = StripeClient(cfg)
        self.auth_on = cfgmod.auth_enabled(cfg)
        self._runway = None
        self._runway_ts = 0

    def runway(self):
        """Cached monitor bridge (refresh at most every 60s)."""
        mon = self.cfg.get("monitor", {})
        if not mon.get("enabled"):
            return None
        now = time.time()
        if now - self._runway_ts > 60:
            self._runway = bridge.runway(mon)
            self._runway_ts = now
        return self._runway


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx


class Handler(BaseHTTPRequestHandler):
    server_version = f"Plutus/{__version__}"

    # ---- helpers -----------------------------------------------------------
    @property
    def ctx(self) -> _Ctx:
        return self.server.ctx

    def _conn(self):
        return db.connect(self.ctx.db_path)

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, default=str), "application/json")

    def _redirect(self, url):
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def _body(self, max_bytes=MAX_BODY_BYTES) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > max_bytes:
            raise _BodyTooLarge(f"request body {n} bytes exceeds limit {max_bytes}")
        return self.rfile.read(n) if n else b""

    def _form(self) -> dict:
        raw = self._body().decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def log_message(self, fmt, *args):  # quieter, single-line
        sys.stderr.write("plutus %s — %s\n" % (self.address_string(), fmt % args))

    # ---- auth --------------------------------------------------------------
    def _gate(self, conn, path) -> bool:
        """When auth is on, require a session for non-public paths.

        Sets ``self._user`` (row or None). Returns True if the request may
        proceed, False if a 401/redirect was already sent.
        """
        self._user = None
        if not self.ctx.auth_on:
            return True
        self._user = authmod.current_user(self, conn)
        if self._user or path in _PUBLIC_PATHS:
            return True
        # not signed in, protected path
        if path.startswith("/api/") or self.command == "POST":
            self._json(401, {"error": "authentication required"})
        else:
            self._redirect("/auth/login")
        return False

    def _scoped_orgs(self, conn):
        """Orgs visible to this request (all when auth is off)."""
        if self._user is not None:
            return db.list_orgs_for_email(conn, self._user["email"])
        return db.list_orgs(conn)

    def _authz_org(self, conn, requested):
        """Resolve the org for this request, enforcing membership when signed in.

        Returns an org_id, or None meaning "no org available". Raises
        :class:`PermissionError` if a signed-in user asks for an org they are
        not a member of.
        """
        if self._user is None:
            return requested or api.default_org_id(conn)
        orgs = db.list_orgs_for_email(conn, self._user["email"])
        ids = {o["id"] for o in orgs}
        if requested:
            if requested not in ids:
                raise PermissionError(requested)
            return requested
        return orgs[0]["id"] if orgs else None

    def _auth_login(self):
        try:
            url = authmod.login_url(self.ctx.cfg)
        except authmod.AuthError as e:
            return self._send(500, views.simple_page(
                "Sign in", "Sign-in is misconfigured", html.escape(str(e)), ok=False))
        return self._send(200, views.login_page(url))

    def _auth_callback(self, conn, q):
        flat = {k: (v[0] if isinstance(v, list) else v) for k, v in q.items()}
        try:
            token = authmod.handle_callback(conn, self.ctx.cfg, flat)
        except authmod.AuthError as e:
            return self._send(403, views.simple_page(
                "Sign in", "Could not sign you in", html.escape(str(e)), ok=False))
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", authmod.set_cookie_header(token, self.ctx.cfg))
        self.end_headers()

    def _auth_logout(self, conn):
        db.delete_session(conn, authmod.read_cookie(self))
        self.send_response(303)
        self.send_header("Location", "/auth/login")
        self.send_header("Set-Cookie", authmod.clear_cookie_header(self.ctx.cfg))
        self.end_headers()

    def _same_origin(self) -> bool:
        """CSRF defense: the request's Origin/Referer host must match our own.

        Prefer the configured ``auth.base_url``; if it's unset, fall back to the
        request's own ``Host`` header so we still **fail closed** — Fix #32: the
        previous code returned ``True`` (allowing any cross-origin POST) whenever
        ``base_url`` was unconfigured. A request carrying neither Origin nor
        Referer is rejected.
        """
        base_url = (self.ctx.cfg.get("auth", {}).get("base_url") or "").rstrip("/")
        expected = (urlparse(base_url).netloc.lower() if base_url
                    else (self.headers.get("Host") or "").lower())
        if not expected:
            return False
        # Origin is preferred; Referer is the fallback. A present-but-mismatched
        # Origin blocks even if Referer would match.
        for header in ("Origin", "Referer"):
            val = self.headers.get(header, "")
            if val:
                return urlparse(val).netloc.lower() == expected
        return False

    # ---- routing -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        conn = self._conn()
        try:
            if not self._gate(conn, path):
                return
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": __version__,
                                        "demo": self.ctx.demo})
            if path == "/auth/login":
                return self._auth_login()
            if path == "/auth/callback":
                return self._auth_callback(conn, q)
            # /auth/logout moved to POST for CSRF protection
            if path == "/auth/logout":
                return self._json(405, {"error": "use POST to logout"})
            if path == "/pricing":
                return self._pricing(conn)
            if path == "/api/orgs":
                return self._json(200, api.orgs_json(conn, self._scoped_orgs(conn)))
            if path == "/api/summary":
                org_id = self._authz_org(conn, q.get("org", [None])[0])
                if not org_id:
                    return self._json(404, {"error": "no organizations"})
                return self._json(200, api.summary_json(conn, org_id))
            if path in ("/billing/success", "/billing/cancel"):
                ok = path.endswith("success")
                return self._send(200, views.simple_page(
                    "Billing", "Payment complete ✓" if ok else "Checkout canceled",
                    "Your credit balance updates as soon as Stripe confirms the payment "
                    "(via webhook). You can close this tab." if ok else
                    "No charge was made.", ok=ok))
            if path == "/" or path == "/index.html":
                return self._dashboard(conn, q)
            return self._send(404, views.simple_page("Not found", "404",
                              f"No route for <span class='num'>{path}</span>.", ok=False))
        except PermissionError:
            return self._json(403, {"error": "forbidden: not a member of that org"})
        except Exception as e:  # never 500 silently
            sys.stderr.write(f"plutus: GET {path} failed: {e}\n")
            return self._send(500, views.simple_page("Error", "Something broke",
                              str(e), ok=False))
        finally:
            conn.close()

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        conn = self._conn()
        try:
            if not self._gate(conn, path):
                return
            
            # CSRF protection: cookie-authenticated state-changing POSTs must be same-origin
            # Exempt: /v1/usage (Bearer auth), /webhook/stripe (signature-verified)
            needs_csrf_check = (
                self.ctx.auth_on and 
                path not in {"/v1/usage", "/webhook/stripe"} and
                self._user is not None  # Cookie-authenticated
            )
            if needs_csrf_check and not self._same_origin():
                return self._json(403, {"error": "cross-origin request blocked"})
            
            if path == "/auth/logout":
                return self._auth_logout(conn)
            if path == "/v1/usage":
                return self._ingest_usage(conn)
            if path == "/webhook/stripe":
                return self._webhook(conn)
            if path == "/billing/checkout/credit":
                return self._checkout_credit(conn)
            if path == "/billing/checkout/pro":
                return self._checkout_pro(conn)
            if path == "/billing/portal":
                return self._portal(conn)
            if path == "/keys/create":
                return self._keys_create(conn)
            if path == "/keys/revoke":
                return self._keys_revoke(conn)
            return self._json(404, {"error": f"no route for {path}"})
        except _BodyTooLarge:
            return self._json(413, {"error": "request body too large"})
        except PermissionError:
            return self._json(403, {"error": "forbidden: not a member of that org"})
        except BillingError as e:
            return self._send(400, views.simple_page("Billing", "Billing not available",
                              str(e), ok=False))
        except Exception as e:
            sys.stderr.write(f"plutus: POST {path} failed: {e}\n")
            return self._json(500, {"error": str(e)})
        finally:
            conn.close()

    # ---- views -------------------------------------------------------------
    def _dashboard(self, conn, q):
        org_id = self._authz_org(conn, q.get("org", [None])[0])
        if not org_id:
            return self._send(200, views.simple_page(
                "Plutus", "No organization yet",
                "Run <span class='num'>plutus init</span> to create one, or "
                "<span class='num'>plutus serve --demo</span> to explore with sample data.",
            ))
        from .. import metering
        summary = metering.org_summary(conn, org_id)
        page = views.render_dashboard(
            summary, orgs=self._scoped_orgs(conn), cfg=self.ctx.cfg,
            stripe_status=self.ctx.stripe.status(), demo=self.ctx.demo,
            runway=self.ctx.runway(), user=self._user,
            api_keys=[dict(k) for k in db.list_api_keys(conn, org_id)],
        )
        return self._send(200, page)

    def _pricing(self, conn):
        signed_in = (not self.ctx.auth_on) or (self._user is not None)
        org_id = None
        if signed_in:
            try:
                org_id = self._authz_org(conn, None)
            except PermissionError:
                org_id = None
        return self._send(200, views.pricing_page(
            stripe_status=self.ctx.stripe.status(),
            org_id=org_id, user=self._user, signed_in=signed_in))

    # ---- ingest API --------------------------------------------------------
    def _bearer_org(self, conn):
        """Resolve the org from an ``Authorization: Bearer plutus_sk_…`` header."""
        h = self.headers.get("Authorization", "")
        if h[:7].lower() != "bearer ":
            return None
        return db.api_key_org(conn, h[7:].strip())

    def _ingest_usage(self, conn):
        """POST /v1/usage — meter one event (or a JSON array of them) via API key.

        Returns the metered result(s). When an org on a limited tier is past its
        quota and hard-blocking is on, the event is rejected with 402.
        """
        org_id = self._bearer_org(conn)
        if not org_id:
            return self._json(401, {"error": "invalid or missing API key"})
        try:
            body = self._body()
        except _BodyTooLarge:
            raise
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            return self._json(400, {"error": "body must be JSON"})
        events = payload if isinstance(payload, list) else [payload]
        if not events or len(events) > 1000:
            return self._json(400, {"error": "send 1–1000 events"})

        from .. import metering
        cfg = self.ctx.cfg
        block_free = bool(cfg.get("pricing", {}).get("block_over_free_limit"))
        block_balance = bool(cfg.get("pricing", {}).get("block_over_balance"))
        
        # Fix #27: validate ALL events before recording ANY
        for ev in events:
            if not isinstance(ev, dict) or not ev.get("provider"):
                return self._json(400, {"error": "each event needs a 'provider'"})
            # Validate int fields can coerce
            try:
                int(ev.get("input_tokens", 0) or 0)
                int(ev.get("output_tokens", 0) or 0)
                int(ev.get("cache_read_tokens", 0) or 0)
                int(ev.get("reasoning_tokens", 0) or 0)
            except (TypeError, ValueError):
                return self._json(400, {"error": "token fields must be integers"})
        
        # All valid — record the whole batch as one serialized transaction.
        # Fix #27/#30: db.immediate() takes the write lock up front (BEGIN
        # IMMEDIATE) so the per-event quota / prepaid hard-stop reads can't race
        # a concurrent writer between the read and the insert, and commits once
        # (no partial batch). record_usage(commit=False) defers to that commit.
        out, n_blocked, n_over_balance = [], 0, 0
        try:
            with db.immediate(conn):
                for ev in events:
                    res = metering.record_usage(
                        conn, org_id, provider=str(ev["provider"]),
                        model=ev.get("model"), task_type=ev.get("task_type", "general"),
                        workspace=ev.get("workspace"),
                        input_tokens=strict_int(ev.get("input_tokens", 0) or 0),
                        output_tokens=strict_int(ev.get("output_tokens", 0) or 0),
                        cache_read_tokens=strict_int(ev.get("cache_read_tokens", 0) or 0),
                        reasoning_tokens=strict_int(ev.get("reasoning_tokens", 0) or 0),
                        cost_usd=ev.get("cost_usd"),
                        source=ev.get("source", "api"),
                        pricing_overrides=cfg.get("pricing", {}).get("overrides"),
                        alert_cfg=cfg.get("alerts", {}),
                        block_over_limit=block_free,
                        block_over_balance=block_balance,
                        commit=False,  # defer commit to db.immediate()
                    )
                    if not res.recorded:
                        if res.over_balance:
                            n_over_balance += 1
                        else:
                            n_blocked += 1
                    out.append({
                        "recorded": res.recorded,
                        "event_id": res.event_id or None,
                        "cost_usd": res.cost_usd,
                        "estimated": res.estimated,
                        "balance_after": res.balance_after,
                        "over_free_limit": res.over_free_limit,
                        "over_balance": res.over_balance,
                    })
        except Exception:
            return self._json(400, {"error": "batch recording failed"})

        st = metering.tier_status(conn, org_id)
        body = {
            "org_id": org_id,
            "tracked_tokens_mtd": st["tracked_tokens"],
            "tracked_limit": st["tracked_limit"],
            "tier": st["tier"],
        }
        if len(out) == 1:
            body.update(out[0])
        else:
            body["results"] = out
            body["recorded"] = len(out) - n_blocked - n_over_balance
            body["blocked"] = n_blocked
        
        # 402 when nothing landed (either free quota exhausted or balance exhausted)
        if n_blocked == len(out):
            base = (cfg.get("auth", {}).get("base_url") or "").rstrip("/")
            body["error"] = "free-tier token quota reached — upgrade to Pro"
            body["upgrade_url"] = (base + "/pricing") if base else "/pricing"
            return self._json(402, body)
        if n_over_balance == len(out):
            base = (cfg.get("auth", {}).get("base_url") or "").rstrip("/")
            body["error"] = "prepaid credit exhausted"
            body["upgrade_url"] = (base + "/pricing") if base else "/pricing"
            return self._json(402, body)
        return self._json(200, body)

    # ---- API keys (session-gated; created from the dashboard) --------------
    def _keys_create(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"))
        if not org_id:
            return self._send(400, views.simple_page(
                "API keys", "No organization", "Create an org first.", ok=False))
        _, secret = db.create_api_key(conn, org_id, name=(f.get("name") or "").strip() or None)
        base = (self.ctx.cfg.get("auth", {}).get("base_url") or "").rstrip("/") \
            or f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        return self._send(200, views.api_key_created_page(secret, base))

    def _keys_revoke(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"))
        if org_id and f.get("key_id"):
            db.revoke_api_key(conn, f.get("key_id"), org_id)
        return self._redirect("/")

    # ---- billing -----------------------------------------------------------
    def _checkout_credit(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"))
        if not org_id:
            raise BillingError("no organization to bill")
        amount = float(f.get("amount") or 50)
        sess = self.ctx.stripe.credit_checkout(conn, org_id, amount)
        return self._redirect(sess["url"])

    def _checkout_pro(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"))
        if not org_id:
            raise BillingError("no organization to bill")
        sess = self.ctx.stripe.pro_checkout(conn, org_id)
        return self._redirect(sess["url"])

    def _portal(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"))
        if not org_id:
            raise BillingError("no organization to bill")
        sess = self.ctx.stripe.portal(conn, org_id)
        return self._redirect(sess["url"])

    def _webhook(self, conn):
        payload = self._body()
        sig = self.headers.get("Stripe-Signature", "")
        try:
            event = self.ctx.stripe.construct_event(payload, sig)
        except BillingError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # signature failure etc.
            return self._json(400, {"error": f"invalid webhook: {e}"})
        # Stripe events are dict-like
        result = handle_webhook_event(conn, event)
        sys.stderr.write(f"plutus: stripe event {result}\n")
        return self._json(200, {"received": True, "result": result})


def serve(host=None, port=None, db_path=None, demo=False, cfg=None,
          open_browser=False):
    """Start the dashboard/API server. Blocks until interrupted."""
    cfg = cfg or cfgmod.load()
    host = host or cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(port or cfg.get("server", {}).get("port", 8420))
    db_path = db_path or str(cfgmod.db_path())

    # make sure schema exists
    c = db.connect(db_path)
    db.init_schema(c)
    c.close()

    ctx = _Ctx(cfg, db_path, demo=demo)
    httpd = _Server((host, port), Handler, ctx)
    url = f"http://{host}:{port}/"
    stripe_mode = ctx.stripe.status()["mode"]
    if ctx.auth_on:
        signup = " · open signup" if cfg.get("auth", {}).get("allow_signup") else " · allow-list"
        auth_mode = f"Google OIDC ({cfg['auth'].get('base_url') or 'base_url unset!'}){signup}"
    elif cfg.get("auth", {}).get("enabled"):
        auth_mode = "enabled but NOT configured → open (set Google client id/secret)"
    else:
        auth_mode = "open (no sign-in required)"
    sys.stderr.write(
        f"\n  ◆ Plutus v{__version__} — the billing layer for AI agents\n"
        f"    dashboard:  {url}\n"
        f"    stripe:     {stripe_mode}\n"
        f"    auth:       {auth_mode}\n"
        f"    database:   {db_path}{'  (demo data)' if demo else ''}\n"
        f"    webhook:    POST {url}webhook/stripe\n\n"
        f"  Ctrl-C to stop.\n\n"
    )
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n  plutus: stopped.\n")
    finally:
        httpd.server_close()
