"""HTTP server — stdlib ``http.server`` only. No web framework dependency.

Serves the dark dashboard, a JSON API, Stripe Checkout/Portal redirects, and the
Stripe webhook, all at ``:8420`` by default. Threaded, with a fresh SQLite
connection per request (SQLite connections aren't thread-safe to share).
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .. import __version__, bridge, config as cfgmod, db
from ..billing import StripeClient, BillingError, handle_webhook_event
from . import api, views


class _Ctx:
    """Shared, read-mostly server context."""
    def __init__(self, cfg, db_path, demo=False):
        self.cfg = cfg
        self.db_path = db_path
        self.demo = demo
        self.stripe = StripeClient(cfg)
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

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _form(self) -> dict:
        raw = self._body().decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def log_message(self, fmt, *args):  # quieter, single-line
        sys.stderr.write("plutus %s — %s\n" % (self.address_string(), fmt % args))

    # ---- routing -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        conn = self._conn()
        try:
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": __version__,
                                        "demo": self.ctx.demo})
            if path == "/api/orgs":
                return self._json(200, api.orgs_json(conn))
            if path == "/api/summary":
                org_id = (q.get("org", [None])[0]) or api.default_org_id(conn)
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
            if path == "/webhook/stripe":
                return self._webhook(conn)
            if path == "/billing/checkout/credit":
                return self._checkout_credit(conn)
            if path == "/billing/checkout/pro":
                return self._checkout_pro(conn)
            if path == "/billing/portal":
                return self._portal(conn)
            return self._json(404, {"error": f"no route for {path}"})
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
        org_id = (q.get("org", [None])[0]) or api.default_org_id(conn)
        if not org_id:
            return self._send(200, views.simple_page(
                "Plutus", "No organization yet",
                "Run <span class='num'>plutus init</span> to create one, or "
                "<span class='num'>plutus serve --demo</span> to explore with sample data.",
            ))
        from .. import metering
        summary = metering.org_summary(conn, org_id)
        html = views.render_dashboard(
            summary, orgs=db.list_orgs(conn), cfg=self.ctx.cfg,
            stripe_status=self.ctx.stripe.status(), demo=self.ctx.demo,
            runway=self.ctx.runway(),
        )
        return self._send(200, html)

    # ---- billing -----------------------------------------------------------
    def _checkout_credit(self, conn):
        f = self._form()
        org_id = f.get("org") or api.default_org_id(conn)
        amount = float(f.get("amount") or 50)
        sess = self.ctx.stripe.credit_checkout(conn, org_id, amount)
        return self._redirect(sess["url"])

    def _checkout_pro(self, conn):
        f = self._form()
        org_id = f.get("org") or api.default_org_id(conn)
        sess = self.ctx.stripe.pro_checkout(conn, org_id)
        return self._redirect(sess["url"])

    def _portal(self, conn):
        f = self._form()
        org_id = f.get("org") or api.default_org_id(conn)
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
    sys.stderr.write(
        f"\n  ◆ Plutus v{__version__} — the billing layer for AI agents\n"
        f"    dashboard:  {url}\n"
        f"    stripe:     {stripe_mode}\n"
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
