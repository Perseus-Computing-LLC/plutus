#!/usr/bin/env python3
"""Tests for the Plutus monetization engine (plutus_agent)."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering, pricing, demo, reports
from plutus_agent.billing import handle_webhook_event


_PATHS = {}  # id(conn) -> file path, since sqlite3.Connection forbids attrs


def fresh_conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    db.init_schema(conn)
    _PATHS[id(conn)] = path
    return conn


def drop_conn(conn):
    path = _PATHS.pop(id(conn), None)
    conn.close()
    for ext in ("", "-wal", "-shm"):
        try:
            if path:
                os.unlink(path + ext)
        except OSError:
            pass


class TestPricing(unittest.TestCase):
    def test_estimate_known_model(self):
        # opus: 15/M in, 75/M out
        cost = pricing.estimate_cost("anthropic", "claude-opus-4-8", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 90.0, places=4)

    def test_reasoning_billed_as_output(self):
        c1 = pricing.estimate_cost("anthropic", "claude-opus-4-8", 0, 1000, 0, 0)
        c2 = pricing.estimate_cost("anthropic", "claude-opus-4-8", 0, 0, 0, 1000)
        self.assertAlmostEqual(c1, c2, places=8)

    def test_unknown_provider_falls_back(self):
        self.assertGreater(pricing.estimate_cost("acme-llm", "x", 1_000_000, 0), 0)

    def test_overrides_applied(self):
        ov = {"anthropic": {"claude-opus-4-8": {"input": 1.0, "output": 1.0}}}
        cost = pricing.estimate_cost("anthropic", "claude-opus-4-8", 1_000_000, 0,
                                     overrides=ov)
        self.assertAlmostEqual(cost, 1.0, places=6)

    def test_tiers(self):
        self.assertEqual(pricing.tier("pro").price_usd_month, 20.0)
        self.assertEqual(pricing.tier("free").tracked_tokens_month, 10_000)
        self.assertIsNone(pricing.tier("enterprise").workspaces)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.org = db.create_org(self.conn, "Acme")["id"]

    def tearDown(self):
        drop_conn(self.conn)

    def test_balance_is_running_sum(self):
        self.assertEqual(db.get_balance(self.conn, self.org), 0.0)
        db.add_ledger(self.conn, self.org, 100.0, "topup")
        db.add_ledger(self.conn, self.org, -12.5, "debit")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 87.5, places=4)

    def test_slug_uniqueness(self):
        a = db.create_org(self.conn, "Dup")
        b = db.create_org(self.conn, "Dup")
        self.assertNotEqual(a["slug"], b["slug"])


class TestMetering(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.org = db.create_org(self.conn, "Acme", tier="pro")["id"]

    def tearDown(self):
        drop_conn(self.conn)

    def test_record_creates_workspace_and_event(self):
        r = metering.record_usage(self.conn, self.org, provider="anthropic",
                                  model="claude-opus-4-8", task_type="code_review",
                                  input_tokens=1000, output_tokens=500, workspace="ci")
        self.assertTrue(r.event_id.startswith("evt_"))
        self.assertIsNotNone(r.workspace_id)
        self.assertGreater(r.cost_usd, 0)
        ws = db.list_workspaces(self.conn, self.org)
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["name"], "ci")

    def test_exact_cost_overrides_estimate(self):
        r = metering.record_usage(self.conn, self.org, provider="anthropic",
                                  input_tokens=999999, output_tokens=999999,
                                  cost_usd=0.05)
        self.assertFalse(r.estimated)
        self.assertEqual(r.cost_usd, 0.05)

    def test_credit_depletes(self):
        db.add_ledger(self.conn, self.org, 10.0, "topup")
        metering.record_usage(self.conn, self.org, provider="anthropic",
                              cost_usd=3.0)
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 7.0, places=4)

    def test_low_balance_alert(self):
        db.add_ledger(self.conn, self.org, 10.0, "topup")
        r = metering.record_usage(self.conn, self.org, provider="anthropic",
                                  cost_usd=9.5,
                                  alert_cfg={"low_balance_usd": 5.0})
        kinds = {a["kind"] for a in r.alerts}
        self.assertIn("low_balance", kinds)

    def test_budget_cap_alert(self):
        ws = db.create_workspace(self.conn, self.org, "capped", monthly_budget_usd=1.0)
        r = metering.record_usage(self.conn, self.org, provider="anthropic",
                                  cost_usd=1.5, workspace="capped",
                                  alert_cfg={"budget_warn_pct": 80.0})
        kinds = {a["kind"] for a in r.alerts}
        self.assertIn("budget_cap", kinds)

    def test_windows_and_grouping(self):
        now = time.time()
        metering.record_usage(self.conn, self.org, provider="anthropic",
                              cost_usd=1.0, task_type="chat", ts=now)
        metering.record_usage(self.conn, self.org, provider="google",
                              cost_usd=2.0, task_type="research", ts=now - 10*86400)
        w = metering.org_spend_windows(self.conn, self.org)
        self.assertAlmostEqual(w["today"]["cost"], 1.0, places=4)
        self.assertAlmostEqual(w["30d"]["cost"], 3.0, places=4)
        byp = {x["key"]: x["cost"] for x in metering.spend_by(self.conn, self.org, "provider")}
        self.assertAlmostEqual(byp["google"], 2.0, places=4)

    def test_tracked_tokens_limit_math(self):
        metering.record_usage(self.conn, self.org, provider="x",
                              input_tokens=6000, output_tokens=4000, cost_usd=0)
        self.assertEqual(metering.tracked_tokens_mtd(self.conn, self.org), 10000)


class TestDemo(unittest.TestCase):
    def test_seed_produces_rich_data(self):
        conn = fresh_conn()
        try:
            org_id = demo.seed(conn, events=300)
            s = metering.org_summary(conn, org_id)
            self.assertGreater(s["windows"]["all"]["cost"], 0)
            self.assertGreaterEqual(len(s["by_provider"]), 3)
            self.assertGreaterEqual(len(s["workspaces"]), 4)
            self.assertEqual(s["tier"]["key"], "pro")
        finally:
            drop_conn(conn)


class TestBillingWebhook(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.org = db.create_org(self.conn, "Acme")["id"]
        db.set_stripe_customer(self.conn, self.org, "cus_123")

    def tearDown(self):
        drop_conn(self.conn)

    def test_credit_checkout_completed_tops_up(self):
        event = {
            "id": "evt_1", "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_1", "mode": "payment",
                                 "customer": "cus_123", "amount_total": 5000,
                                 "metadata": {"plutus_org_id": self.org,
                                              "kind": "credit", "amount_usd": "50.00"}}},
        }
        res = handle_webhook_event(self.conn, event)
        self.assertEqual(res["status"], "credited")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 50.0, places=2)

    def test_idempotent(self):
        event = {
            "id": "evt_dupe", "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_2", "mode": "payment",
                                 "customer": "cus_123",
                                 "metadata": {"kind": "credit", "amount_usd": "10.00"}}},
        }
        handle_webhook_event(self.conn, event)
        res2 = handle_webhook_event(self.conn, event)  # replay
        self.assertEqual(res2["status"], "duplicate")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 10.0, places=2)

    def test_subscription_sets_tier(self):
        event = {
            "id": "evt_sub", "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_123", "status": "active"}},
        }
        handle_webhook_event(self.conn, event)
        self.assertEqual(db.get_org(self.conn, self.org)["tier"], "pro")

    def test_subscription_deleted_downgrades(self):
        db.set_org_tier(self.conn, self.org, "pro")
        event = {
            "id": "evt_del", "type": "customer.subscription.deleted",
            "data": {"object": {"customer": "cus_123"}},
        }
        handle_webhook_event(self.conn, event)
        self.assertEqual(db.get_org(self.conn, self.org)["tier"], "free")


class TestReports(unittest.TestCase):
    def test_build_and_render_html(self):
        conn = fresh_conn()
        try:
            org_id = demo.seed(conn, events=200)
            import datetime as dt
            today = dt.date.today()
            rep = reports.build_report(conn, org_id, today.year, today.month)
            self.assertIn("total", rep)
            html = reports.render_html(rep)
            self.assertIn("<!doctype html>", html)
            self.assertIn("Plutus", html)
        finally:
            drop_conn(conn)


class TestClaudeHook(unittest.TestCase):
    def test_merge_is_idempotent(self):
        from plutus_agent import cli
        cmd = cli._hook_command()
        settings = {}
        settings, changed = cli._merge_stop_hook(settings, cmd)
        self.assertTrue(changed)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        settings, changed = cli._merge_stop_hook(settings, cmd)
        self.assertFalse(changed)  # not added twice
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)

    def test_merge_preserves_existing_hooks(self):
        from plutus_agent import cli
        settings = {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "echo existing"}]}]},
            "model": "claude-opus-4-8"}
        settings, changed = cli._merge_stop_hook(settings, cli._hook_command())
        self.assertTrue(changed)
        self.assertEqual(len(settings["hooks"]["Stop"]), 2)
        self.assertEqual(settings["model"], "claude-opus-4-8")  # untouched

    def test_hook_meters_payload(self):
        import os
        from plutus_agent.integrations import claude_code_hook
        d = os.path.join(tempfile.mkdtemp(), "hook.db")
        os.environ["PLUTUS_DB"] = d
        os.environ["PLUTUS_ORG"] = "HookTest"
        try:
            res = claude_code_hook.meter_payload({
                "usage": {"input_tokens": 1000, "output_tokens": 500,
                          "cache_read_input_tokens": 200},
                "model": "claude-opus-4-8", "cwd": "/home/me/proj-x"})
            self.assertGreater(res.cost_usd, 0)
            self.assertEqual(res.task_type, "coding")
        finally:
            os.environ.pop("PLUTUS_DB", None)
            os.environ.pop("PLUTUS_ORG", None)


if __name__ == "__main__":
    unittest.main()
