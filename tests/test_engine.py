#!/usr/bin/env python3
"""Tests for the Plutus monetization engine (plutus_agent)."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering, pricing, demo, reports, config as cfgmod
from plutus_agent.billing import handle_webhook_event
from plutus_agent.utils import strict_int
from plutus_agent.server import auth as authmod


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


class TestFreeTierLimits(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.org = db.create_org(self.conn, "Free Co", tier="free")["id"]

    def tearDown(self):
        drop_conn(self.conn)

    def _meter(self, tokens, **kw):
        return metering.record_usage(self.conn, self.org, provider="anthropic",
                                     input_tokens=tokens, cost_usd=0.0, **kw)

    def test_under_limit_not_flagged(self):
        r = self._meter(5_000)
        self.assertTrue(r.recorded)
        self.assertFalse(r.over_free_limit)

    def test_over_limit_still_records_but_flags(self):
        self._meter(8_000)
        r = self._meter(5_000)  # pushes past the 10K free cap
        self.assertTrue(r.recorded)            # data is never silently dropped
        self.assertTrue(r.over_free_limit)
        self.assertEqual(metering.tracked_tokens_mtd(self.conn, self.org), 13_000)

    def test_block_mode_drops_event_past_cap(self):
        self._meter(10_000)  # exactly at cap
        r = self._meter(1_000, block_over_limit=True)
        self.assertFalse(r.recorded)
        self.assertTrue(r.over_free_limit)
        self.assertEqual(r.event_id, "")
        self.assertEqual(metering.tracked_tokens_mtd(self.conn, self.org), 10_000)

    def test_pro_tier_is_unlimited(self):
        pro = db.create_org(self.conn, "Pro Co", tier="pro")["id"]
        r = metering.record_usage(self.conn, pro, provider="anthropic",
                                  input_tokens=5_000_000, cost_usd=0.0,
                                  block_over_limit=True)
        self.assertTrue(r.recorded)
        self.assertFalse(r.over_free_limit)

    def test_workspace_cap_folds_into_first(self):
        self._meter(100, workspace="alpha")
        self._meter(100, workspace="beta")   # 2nd workspace blocked on Free (cap 1)
        ws = db.list_workspaces(self.conn, self.org)
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["name"], "alpha")

    def test_tier_status_reports_usage(self):
        self._meter(7_600)
        st = metering.tier_status(self.conn, self.org)
        self.assertTrue(st["is_free"])
        self.assertEqual(st["tracked_limit"], 10_000)
        self.assertTrue(st["near_limit"])
        self.assertFalse(st["over_limit"])
        self._meter(3_000)
        st = metering.tier_status(self.conn, self.org)
        self.assertTrue(st["over_limit"])

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
    
    def test_block_over_balance(self):
        """Fix #28: block_over_balance prevents debits past zero."""
        db.add_ledger(self.conn, self.org, 5.0, "topup")
        r = metering.record_usage(self.conn, self.org, provider="anthropic",
                                  cost_usd=10.0, block_over_balance=True)
        self.assertFalse(r.recorded)
        self.assertTrue(r.over_balance)
        self.assertEqual(r.event_id, "")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 5.0, places=4)
    
    def test_block_over_balance_allows_within_limit(self):
        """Fix #28: block_over_balance allows events within balance."""
        db.add_ledger(self.conn, self.org, 10.0, "topup")
        r = metering.record_usage(self.conn, self.org, provider="anthropic",
                                  cost_usd=5.0, block_over_balance=True)
        self.assertTrue(r.recorded)
        self.assertFalse(r.over_balance)
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 5.0, places=4)


class TestApiKeys(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.org = db.create_org(self.conn, "Acme", tier="free")["id"]

    def tearDown(self):
        drop_conn(self.conn)

    def test_create_returns_secret_once_and_resolves(self):
        row, secret = db.create_api_key(self.conn, self.org, name="prod")
        self.assertTrue(secret.startswith("plutus_sk_"))
        self.assertEqual(db.api_key_org(self.conn, secret), self.org)
        # only a hash is stored — the raw secret is nowhere in the row
        self.assertNotIn(secret, dict(row).values())
        self.assertEqual(row["prefix"], secret[:14])

    def test_bad_or_unknown_key_denied(self):
        self.assertIsNone(db.api_key_org(self.conn, "nope"))
        self.assertIsNone(db.api_key_org(self.conn, "plutus_sk_doesnotexist"))
        self.assertIsNone(db.api_key_org(self.conn, ""))

    def test_revoke_blocks_key(self):
        row, secret = db.create_api_key(self.conn, self.org)
        self.assertTrue(db.revoke_api_key(self.conn, row["id"], self.org))
        self.assertIsNone(db.api_key_org(self.conn, secret))
        self.assertEqual(db.list_api_keys(self.conn, self.org), [])
        # revoking again is a no-op
        self.assertFalse(db.revoke_api_key(self.conn, row["id"], self.org))

    def test_revoke_scoped_to_org(self):
        other = db.create_org(self.conn, "Other", tier="free")["id"]
        row, _ = db.create_api_key(self.conn, self.org)
        self.assertFalse(db.revoke_api_key(self.conn, row["id"], other))
        self.assertEqual(len(db.list_api_keys(self.conn, self.org)), 1)

    def test_last_used_touched(self):
        _, secret = db.create_api_key(self.conn, self.org)
        db.api_key_org(self.conn, secret)
        self.assertIsNotNone(db.list_api_keys(self.conn, self.org)[0]["last_used_at"])


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
    
    def test_concurrent_webhook_credits_once(self):
        """Fix #26: concurrent duplicate deliveries credit only once."""
        event = {
            "id": "evt_concurrent", "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_conc", "mode": "payment",
                                 "customer": "cus_123", "amount_total": 2000,
                                 "metadata": {"kind": "credit"}}},
        }
        res1 = handle_webhook_event(self.conn, event)
        res2 = handle_webhook_event(self.conn, event)
        self.assertEqual(res1["status"], "credited")
        self.assertEqual(res2["status"], "duplicate")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 20.0, places=2)
    
    def test_mark_stripe_event_atomicity(self):
        """Fix #26: mark_stripe_event returns True then False for same id."""
        claimed1 = db.mark_stripe_event(self.conn, "evt_atomic", "test.type")
        claimed2 = db.mark_stripe_event(self.conn, "evt_atomic", "test.type")
        self.assertTrue(claimed1)
        self.assertFalse(claimed2)
    
    def test_checkout_prefers_amount_total(self):
        """Fix #29: amount_total wins over metadata.amount_usd."""
        event = {
            "id": "evt_amount", "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_amt", "mode": "payment",
                                 "customer": "cus_123", "amount_total": 5000,
                                 "metadata": {"kind": "credit", "amount_usd": "999.00"}}},
        }
        res = handle_webhook_event(self.conn, event)
        self.assertEqual(res["status"], "credited")
        self.assertAlmostEqual(res["amount_usd"], 50.0, places=2)
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 50.0, places=2)

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


class TestConfigSecretHandling(unittest.TestCase):
    """Regression: env-provided secrets must never be written to config.yaml."""

    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp()
        os.environ["PLUTUS_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("PLUTUS_HOME", None)
        os.environ.pop("STRIPE_SECRET_KEY", None)

    def test_save_strips_env_secret(self):
        from plutus_agent import config as cfgmod
        os.environ["STRIPE_SECRET_KEY"] = "sk_live_should_not_persist"
        cfg = cfgmod.load()  # env-merged — contains the key in memory
        self.assertEqual(cfg["billing"]["stripe_secret_key"], "sk_live_should_not_persist")
        cfg["billing"]["stripe_price_pro"] = "price_123"
        cfgmod.save(cfg)  # must strip the env secret
        import yaml
        on_disk = yaml.safe_load(open(cfgmod.config_path(), encoding="utf-8"))
        self.assertEqual(on_disk["billing"]["stripe_secret_key"], "")
        self.assertEqual(on_disk["billing"]["stripe_price_pro"], "price_123")

    def test_load_base_has_no_env(self):
        from plutus_agent import config as cfgmod
        os.environ["STRIPE_SECRET_KEY"] = "sk_live_env_only"
        self.assertEqual(cfgmod.load_base()["billing"]["stripe_secret_key"], "")
        self.assertEqual(cfgmod.load()["billing"]["stripe_secret_key"], "sk_live_env_only")


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


# ---- Security hardening test (issue #35) ---------------------------------------
class TestSMTPTLSSecurity(unittest.TestCase):
    """Test SMTP TLS enforcement (Fix #35)."""
    
    def test_no_login_without_tls(self):
        """SMTP should not login with credentials when TLS is unavailable."""
        from plutus_agent import alerts
        import unittest.mock as mock
        
        conn = fresh_conn()
        try:
            org = db.create_org(conn, "Test Org")
            
            # Create a pending alert
            conn.execute(
                "INSERT INTO alerts_log (id, org_id, kind, message, ts, delivered) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("alert_1", org["id"], "low_balance", "Test alert", time.time(), 0)
            )
            conn.commit()
            
            # Mock SMTP to simulate a server without STARTTLS support
            with mock.patch("plutus_agent.alerts.smtplib.SMTP") as mock_smtp:
                mock_server = mock.MagicMock()
                mock_server.esmtp_features = {}  # No STARTTLS
                mock_smtp.return_value.__enter__.return_value = mock_server
                
                cfg = {
                    "alerts": {
                        "enabled": True,
                        "smtp_host": "mail.example.com",
                        "smtp_port": 25,
                        "smtp_user": "testuser",
                        "smtp_password": "testpass",
                        "to_addrs": ["test@example.com"]
                    }
                }
                
                result = alerts.send_pending(conn, cfg, org["id"])
                
                # Should return an error about TLS
                self.assertIn("error", result)
                self.assertIn("TLS", result["error"])
                self.assertEqual(result["sent"], 0)
                
                # Verify login was NOT called
                mock_server.login.assert_not_called()
        finally:
            drop_conn(conn)


class TestCliDbFlag(unittest.TestCase):
    """Fix #47: `plutus --db <path>` must not crash (cli.py used os without
    importing it, so any invocation with --db raised NameError)."""

    def test_db_flag_sets_env_and_runs(self):
        from plutus_agent import cli
        d = os.path.join(tempfile.mkdtemp(), "cli.db")
        prev = os.environ.pop("PLUTUS_DB", None)
        try:
            rc = cli.main(["--db", d, "version"])  # would NameError before the fix
            self.assertEqual(rc, 0)
            self.assertEqual(os.environ["PLUTUS_DB"], d)
        finally:
            if prev is not None:
                os.environ["PLUTUS_DB"] = prev
            else:
                os.environ.pop("PLUTUS_DB", None)


if __name__ == "__main__":
    unittest.main()
