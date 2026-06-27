#!/usr/bin/env python3
"""#66: CSV/JSON spend export and cursor pagination on ledger + events."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import api, app


class TestPaginationUnit(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Acme")["id"]

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_ledger_cursor_pages_without_overlap(self):
        for i in range(10):
            db.add_ledger(self.conn, self.org, 1.0, "topup", ts=1000.0 + i)
        p1 = api.ledger_json(self.conn, self.org, limit=4)
        self.assertEqual(len(p1["items"]), 4)
        self.assertIsNotNone(p1["next_before"])
        p2 = api.ledger_json(self.conn, self.org, limit=4, before=p1["next_before"])
        ids1 = {r["id"] for r in p1["items"]}
        ids2 = {r["id"] for r in p2["items"]}
        self.assertEqual(ids1 & ids2, set(), "pages must not overlap")
        # last page is short → next_before is None
        p3 = api.ledger_json(self.conn, self.org, limit=4, before=p2["next_before"])
        self.assertEqual(len(p3["items"]), 2)
        self.assertIsNone(p3["next_before"])

    def test_events_cursor_pages(self):
        for _ in range(7):
            metering.record_usage(self.conn, self.org, provider="anthropic",
                                  model="claude-opus-4-8", input_tokens=10,
                                  output_tokens=10)
        p1 = api.events_json(self.conn, self.org, limit=3)
        self.assertEqual(len(p1["items"]), 3)
        p2 = api.events_json(self.conn, self.org, limit=3, before=p1["next_before"])
        self.assertEqual(
            {r["id"] for r in p1["items"]} & {r["id"] for r in p2["items"]}, set())

    def test_orgs_limit_offset(self):
        for n in range(4):
            db.create_org(self.conn, f"Org{n}")
        page = api.orgs_json(self.conn, limit=2, offset=1)
        self.assertEqual(len(page), 2)


class TestExportHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org = db.create_org(conn, "Acme", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org)
        conn.close()
        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        # record three events
        for _ in range(3):
            cls._post_usage(cls)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post_usage(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/usage",
            data=json.dumps({"provider": "anthropic", "model": "claude-opus-4-8",
                             "input_tokens": 100, "output_tokens": 50}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()

    def _get(self, path, key=True):
        headers = {"Authorization": f"Bearer {self.key}"} if key else {}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=headers, method="GET")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode()

    def test_export_csv(self):
        status, ctype, text = self._get("/v1/usage/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        self.assertEqual(lines[0].split(",")[0], "id")  # header
        self.assertEqual(len(lines), 1 + 3)              # header + 3 events

    def test_export_json(self):
        status, ctype, text = self._get("/v1/usage/export.json")
        self.assertEqual(status, 200)
        body = json.loads(text)
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["events"]), 3)

    def test_export_requires_key(self):
        status, _, _ = self._get("/v1/usage/export.csv", key=False)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
