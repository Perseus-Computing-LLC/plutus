#!/usr/bin/env python3
"""Tests for issue #38: integer micro-dollar money storage.

Covers three guarantees:
  1. usd<->micros conversion round-trips and rounds correctly.
  2. Money is stored as INTEGER micros and sums exactly (no float drift) over a
     large ledger -- the whole reason for the migration.
  3. A legacy v3 database (REAL USD columns) migrates idempotently to v4 micros
     with values preserved.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering


def _fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    db.init_schema(conn)
    return conn, path


class TestConversion(unittest.TestCase):
    def test_round_trip(self):
        for usd in (0.0, 0.000001, 0.01, 1.0, 3.14159, 12345.678901, 0.142):
            self.assertEqual(db.micros_to_usd(db.usd_to_micros(usd)), usd)

    def test_rounding_to_nearest_micro(self):
        # 0.0000005 -> rounds to nearest micro; sub-micro precision is dropped.
        self.assertEqual(db.usd_to_micros(0.0000004), 0)
        self.assertEqual(db.usd_to_micros(0.0000006), 1)
        self.assertEqual(db.usd_to_micros(1.2345678), 1234568)  # 7th decimal rounds

    def test_none_passthrough(self):
        self.assertIsNone(db.usd_to_micros(None))
        self.assertIsNone(db.micros_to_usd(None))

    def test_constant(self):
        self.assertEqual(db.MICROS_PER_USD, 1_000_000)


class TestExactSummation(unittest.TestCase):
    """A float REAL ledger drifts when summing many sub-cent rows; integer
    micros must sum exactly."""

    def test_no_drift_over_many_small_debits(self):
        conn, path = _fresh()
        try:
            org = db.create_org(conn, "Acme")["id"]
            db.add_ledger(conn, org, 100.0, "topup")
            # 10,000 debits of $0.001 each = exactly $10.00.
            for _ in range(10_000):
                db.add_ledger(conn, org, -0.001, "debit", commit=False)
            conn.commit()
            bal = db.get_balance(conn, org)
            self.assertEqual(bal, 90.0)                 # exact, no 89.9999...
            self.assertEqual(db.get_balance_micros(conn, org), 90_000_000)
        finally:
            conn.close()
            os.unlink(path)

    def test_money_columns_are_integer(self):
        conn, path = _fresh()
        try:
            cols = {r["name"]: r["type"]
                    for r in conn.execute("PRAGMA table_info(credit_ledger)")}
            self.assertEqual(cols["delta_micros"], "INTEGER")
            self.assertEqual(cols["balance_after_micros"], "INTEGER")
            self.assertNotIn("delta_usd", cols)
            ucols = {r["name"]: r["type"]
                     for r in conn.execute("PRAGMA table_info(usage_events)")}
            self.assertEqual(ucols["cost_micros"], "INTEGER")
            self.assertNotIn("cost_usd", ucols)
        finally:
            conn.close()
            os.unlink(path)

    def test_metering_stores_micros(self):
        conn, path = _fresh()
        try:
            org = db.create_org(conn, "Acme")["id"]
            metering.record_usage(conn, org, provider="anthropic",
                                 input_tokens=1000, output_tokens=500,
                                 cost_usd=0.123456)
            row = conn.execute("SELECT cost_micros FROM usage_events").fetchone()
            self.assertEqual(row["cost_micros"], 123456)
            self.assertIsInstance(row["cost_micros"], int)
        finally:
            conn.close()
            os.unlink(path)


# Legacy v3 schema (REAL money columns) for the migration test.
_V3_SCHEMA = """
CREATE TABLE organizations (id TEXT PRIMARY KEY, name TEXT, slug TEXT,
    tier TEXT DEFAULT 'free', stripe_customer_id TEXT, created_at REAL);
CREATE TABLE workspaces (id TEXT PRIMARY KEY, org_id TEXT, name TEXT, slug TEXT,
    monthly_budget_usd REAL, created_at REAL);
CREATE TABLE usage_events (id TEXT PRIMARY KEY, org_id TEXT, workspace_id TEXT,
    provider TEXT, model TEXT, task_type TEXT, input_tokens INTEGER,
    output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0, estimated INTEGER, source TEXT, ts REAL);
CREATE TABLE credit_ledger (id TEXT PRIMARY KEY, org_id TEXT, delta_usd REAL,
    kind TEXT, reason TEXT, stripe_ref TEXT, balance_after REAL, ts REAL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


class TestMigration(unittest.TestCase):
    def _make_v3(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_V3_SCHEMA)
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','3')")
        conn.execute("INSERT INTO organizations(id,name,slug,created_at) VALUES('org_1','Acme','acme',0)")
        conn.execute("INSERT INTO workspaces(id,org_id,name,slug,monthly_budget_usd,created_at)"
                     " VALUES('ws_1','org_1','Main','main',50.0,0)")
        conn.execute("INSERT INTO usage_events(id,org_id,cost_usd,ts) VALUES('e1','org_1',0.123456,1)")
        conn.execute("INSERT INTO credit_ledger(id,org_id,delta_usd,kind,balance_after,ts)"
                     " VALUES('l1','org_1',100.0,'topup',100.0,1)")
        conn.execute("INSERT INTO credit_ledger(id,org_id,delta_usd,kind,balance_after,ts)"
                     " VALUES('l2','org_1',-0.50,'debit',99.5,2)")
        conn.commit()
        conn.close()
        return path

    def test_v3_migrates_to_micros(self):
        path = self._make_v3()
        try:
            conn = db.connect(path)
            db.init_schema(conn)   # triggers _migrate_money_to_micros
            # Values converted exactly.
            self.assertEqual(
                conn.execute("SELECT cost_micros FROM usage_events").fetchone()["cost_micros"],
                123456)
            self.assertEqual(db.get_balance(conn, "org_1"), 99.5)
            self.assertEqual(db.get_balance_micros(conn, "org_1"), 99_500_000)
            ws = db.get_workspace(conn, "ws_1")
            self.assertEqual(ws["monthly_budget_usd"], 50.0)
            self.assertEqual(ws["monthly_budget_micros"], 50_000_000)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                    .fetchone()["value"], str(db.SCHEMA_VERSION))
            conn.close()
        finally:
            os.unlink(path)

    def test_migration_is_idempotent(self):
        path = self._make_v3()
        try:
            conn = db.connect(path)
            db.init_schema(conn)
            db.init_schema(conn)   # second run must not double-convert or error
            db.init_schema(conn)
            self.assertEqual(db.get_balance_micros(conn, "org_1"), 99_500_000)
            conn.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
