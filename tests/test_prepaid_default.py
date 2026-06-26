#!/usr/bin/env python3
"""#28: the prepaid-credit hard-stop is ON by default, with a per-org
allow_negative_balance escape hatch for trusted/internal track-only orgs."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import cli, db, metering
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app


class TestDefaultOn(unittest.TestCase):
    def test_default_config_blocks_over_balance(self):
        self.assertTrue(DEFAULT_CONFIG["pricing"]["block_over_balance"])


class TestExemption(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Prepaid", tier="pro")["id"]
        db.add_ledger(self.conn, self.org, 1.0, "topup")  # $1 credit

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_hardstop_rejects_when_enforced(self):
        res = metering.record_usage(self.conn, self.org, provider="anthropic",
                                    cost_usd=10.0, block_over_balance=True)
        self.assertFalse(res.recorded)
        self.assertTrue(res.over_balance)
        self.assertGreaterEqual(db.get_balance(self.conn, self.org), 0)

    def test_exempt_org_records_into_negative(self):
        db.set_org_allow_negative(self.conn, self.org, True)
        res = metering.record_usage(self.conn, self.org, provider="anthropic",
                                    cost_usd=10.0, block_over_balance=True)
        self.assertTrue(res.recorded)
        self.assertLess(db.get_balance(self.conn, self.org), 0)

    def test_no_credit_org_never_blocked(self):
        # An org that never held credit keeps full tracking regardless.
        free = db.create_org(self.conn, "FreeCo", tier="free")["id"]
        res = metering.record_usage(self.conn, free, provider="anthropic",
                                    cost_usd=5.0, block_over_balance=True)
        self.assertTrue(res.recorded)


class TestColumnMigration(unittest.TestCase):
    def test_alter_adds_missing_column(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            # a pre-#28 organizations table, no allow_negative_balance column
            conn.execute("CREATE TABLE organizations (id TEXT PRIMARY KEY, "
                         "name TEXT, slug TEXT, tier TEXT, stripe_customer_id "
                         "TEXT, created_at REAL)")
            conn.execute("INSERT INTO organizations VALUES "
                         "('o1','N','n','free',NULL,0)")
            conn.commit()
            self.assertNotIn("allow_negative_balance", db._table_columns(conn, "organizations"))
            db._migrate_add_columns(conn)
            conn.commit()
            cols = db._table_columns(conn, "organizations")
            self.assertIn("allow_negative_balance", cols)
            row = conn.execute("SELECT allow_negative_balance FROM organizations "
                               "WHERE id='o1'").fetchone()
            self.assertEqual(row["allow_negative_balance"], 0)
            conn.close()
        finally:
            os.unlink(path)


class TestCliToggle(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["PLUTUS_DB"] = self.dbpath
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        self.org = db.create_org(conn, "Acme")["id"]
        conn.close()

    def tearDown(self):
        os.environ.pop("PLUTUS_DB", None)
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def _flag(self):
        conn = db.connect(self.dbpath)
        v = db.get_org(conn, self.org)["allow_negative_balance"]
        conn.close()
        return v

    def test_allow_then_enforce(self):
        cli.cmd_org(types.SimpleNamespace(action="allow-negative", name="Acme"))
        self.assertEqual(self._flag(), 1)
        cli.cmd_org(types.SimpleNamespace(action="enforce-balance", name="Acme"))
        self.assertEqual(self._flag(), 0)


class TestServerDefaultOn(unittest.TestCase):
    """End-to-end: an unconfigured server (plain DEFAULT_CONFIG) now hard-stops
    a credited org that would go negative."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Prepaid Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        db.add_ledger(conn, cls.org_id, 1.0, "topup")
        conn.close()
        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=False)  # no override
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

    def test_default_server_returns_402_over_balance(self):
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps({"provider": "anthropic", "cost_usd": 10.0}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 402)
        body = json.loads(cm.exception.read().decode())
        self.assertTrue(body["over_balance"])


if __name__ == "__main__":
    unittest.main()
