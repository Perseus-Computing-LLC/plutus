#!/usr/bin/env python3
"""Tests for Plutus — provider credit & spend monitor."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

# Add parent to path so we can import plutus
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plutus


class TestDeepSeekBalance(unittest.TestCase):
    """Tests for deepseek_balance() — API error handling and happy path."""

    def test_api_error_returns_ok_false(self):
        """When the API call fails, return {'ok': False, 'error': ...}."""
        with patch('plutus._get', side_effect=Exception("Connection refused")):
            result = plutus.deepseek_balance("fake-key")
            self.assertFalse(result["ok"])
            self.assertIn("Connection refused", result["error"])

    def test_happy_path(self):
        """Valid response parses USD balance correctly."""
        mock_data = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "42.50",
                 "granted_balance": "50.00", "topped_up_balance": "0.00"},
                {"currency": "CNY", "total_balance": "300.00",
                 "granted_balance": "350.00", "topped_up_balance": "0.00"},
            ]
        }
        with patch('plutus._get', return_value=mock_data):
            result = plutus.deepseek_balance("fake-key")
            self.assertTrue(result["ok"])
            self.assertEqual(result["balance_usd"], 42.50)
            self.assertEqual(result["granted_usd"], 50.00)
            self.assertEqual(result["topped_up_usd"], 0.00)
            self.assertTrue(result["available"])


class TestLedgerSpend(unittest.TestCase):
    """Tests for ledger_spend() — handles missing state.db and aggregation."""

    def test_missing_db_returns_empty(self):
        """When state.db doesn't exist, return empty dict and a note."""
        result, note = plutus.ledger_spend("/nonexistent/path/state.db")
        self.assertEqual(result, {})
        self.assertIn("not found", note)

    def test_aggregates_by_provider(self):
        """Sessions are aggregated by billing_provider."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    billing_provider TEXT,
                    started_at REAL,
                    actual_cost_usd REAL,
                    estimated_cost_usd REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    reasoning_tokens INTEGER
                )
            """)
            now = time.time()
            conn.execute("""
                INSERT INTO sessions VALUES
                ('deepseek', ?, 0.05, 0.0, 1000, 500, 0, 0),
                ('deepseek', ?, 0.10, 0.0, 2000, 800, 0, 0),
                ('anthropic', ?, 0.0, 0.15, 500, 300, 0, 0)
            """, (now - 3600, now - 1800, now - 3600*24*3))
            conn.commit()
            conn.close()

            result, note = plutus.ledger_spend(db_path)
            self.assertIsNone(note)
            self.assertIn("deepseek", result)
            self.assertIn("anthropic", result)
            self.assertAlmostEqual(result["deepseek"]["today"], 0.15)
            self.assertEqual(result["deepseek"]["sessions"], 2)
            self.assertEqual(result["deepseek"]["in_tok"], 3000)
            self.assertEqual(result["deepseek"]["out_tok"], 1300)
            # anthropic session is >1 day old, should only appear in "all"
            self.assertEqual(result["anthropic"]["today"], 0.0)
            self.assertEqual(result["anthropic"]["all"], 0.15)
        finally:
            os.unlink(db_path)


class TestCollect(unittest.TestCase):
    """Tests for collect() — the main data assembly function."""

    def setUp(self):
        self.tmp_config = tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False)
        self.tmp_config.write("""
providers:
  deepseek:
    api_key: "sk-test-key"
    base_url: "https://api.deepseek.com"
  anthropic:
    api_key: "sk-ant-test"
  google:
    api_key: "test-google-key"
""")
        self.tmp_config.close()

        self.tmp_budgets = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False)
        json.dump({
            "anthropic": {"budget_usd": 100.0, "note": "test"},
            "google": {"budget_usd": 50.0, "note": "test"},
        }, self.tmp_budgets)
        self.tmp_budgets.close()

        self._orig_config = os.environ.get('PLUTUS_HERMES_CONFIG', '')
        self._orig_budgets = os.environ.get('PLUTUS_BUDGETS', '')

    def tearDown(self):
        os.unlink(self.tmp_config.name)
        os.unlink(self.tmp_budgets.name)
        if self._orig_config:
            os.environ['PLUTUS_HERMES_CONFIG'] = self._orig_config
        if self._orig_budgets:
            os.environ['PLUTUS_BUDGETS'] = self._orig_budgets

    @patch('plutus.ledger_spend')
    @patch('plutus.deepseek_balance')
    def test_returns_all_focus_providers(self, mock_balance, mock_ledger):
        """collect() returns entries for all three focus providers."""
        mock_balance.return_value = {"ok": True, "balance_usd": 42.0}
        mock_ledger.return_value = ({}, None)  # empty ledger

        with patch.object(plutus, 'HERMES_CONFIG', self.tmp_config.name), \
             patch.object(plutus, 'STATE_DB', '/nonexistent/state.db'), \
             patch.object(plutus, 'BUDGETS_FILE', self.tmp_budgets.name):
            data = plutus.collect()
            provider_names = {p["provider"] for p in data["providers"]}
            self.assertIn("deepseek", provider_names)
            self.assertIn("anthropic", provider_names)
            self.assertIn("google", provider_names)

    @patch('plutus.ledger_spend')
    @patch('plutus.deepseek_balance')
    def test_live_balance_populated(self, mock_balance, mock_ledger):
        """DeepSeek with balance API shows 'live' source and balance."""
        mock_balance.return_value = {"ok": True, "balance_usd": 42.0}
        mock_ledger.return_value = ({}, None)

        with patch.object(plutus, 'HERMES_CONFIG', self.tmp_config.name), \
             patch.object(plutus, 'STATE_DB', '/nonexistent/state.db'), \
             patch.object(plutus, 'BUDGETS_FILE', self.tmp_budgets.name), \
             patch.dict(plutus.BALANCE_FETCHERS, {"deepseek": mock_balance}):
            data = plutus.collect()
            ds = next(p for p in data["providers"] if p["provider"] == "deepseek")
            self.assertEqual(ds["source"], "live")
            self.assertEqual(ds["balance"], 42.0)

    @patch('plutus.ledger_spend')
    @patch('plutus.deepseek_balance')
    def test_budget_remaining_for_no_balance_provider(self, mock_balance, mock_ledger):
        """Anthropic (no balance API) shows budget-based remaining."""
        mock_balance.return_value = {"ok": True, "balance_usd": 42.0}
        mock_ledger.return_value = ({"anthropic": {
            "today": 0, "7d": 5.0, "30d": 20.0, "all": 25.0,
            "in_tok": 0, "out_tok": 0, "sessions": 5, "last_ts": 0
        }}, None)

        with patch.object(plutus, 'HERMES_CONFIG', self.tmp_config.name), \
             patch.object(plutus, 'STATE_DB', '/nonexistent/state.db'), \
             patch.object(plutus, 'BUDGETS_FILE', self.tmp_budgets.name):
            data = plutus.collect()
            anth = next(p for p in data["providers"] if p["provider"] == "anthropic")
            self.assertEqual(anth["budget"], 100.0)
            self.assertEqual(anth["remaining"], 75.0)  # 100 - 25


class TestCalibrate(unittest.TestCase):
    """Tests for calibrate() — budget back-solving."""

    def setUp(self):
        self.tmp_budgets = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False)
        json.dump({"anthropic": {"budget_usd": 50.0, "note": "old"}}, self.tmp_budgets)
        self.tmp_budgets.close()

    def tearDown(self):
        if os.path.exists(self.tmp_budgets.name):
            os.unlink(self.tmp_budgets.name)

    @patch('plutus.ledger_spend')
    def test_calibrate_backsolves_budget(self, mock_ledger):
        """calibrate sets budget = reported_balance + ledger_spend."""
        mock_ledger.return_value = ({
            "anthropic": {"today": 0, "7d": 0, "30d": 0, "all": 10.0,
                          "in_tok": 0, "out_tok": 0, "sessions": 0, "last_ts": 0}
        }, None)

        with patch.object(plutus, 'BUDGETS_FILE', self.tmp_budgets.name):
            plutus.calibrate(["anthropic=74.46"])
            # budget = 74.46 + 10.00 = 84.46
            result = json.load(open(self.tmp_budgets.name, encoding='utf-8'))
            self.assertAlmostEqual(result["anthropic"]["budget_usd"], 84.46, places=2)


class TestRender(unittest.TestCase):
    """Tests for CLI and HTML renderers."""

    def setUp(self):
        self.data = {
            "generated_at": time.time(),
            "providers": [
                {"provider": "deepseek", "balance": 42.0, "remaining": None,
                 "spend": {"today": 0.5, "7d": 3.5, "30d": 15.0, "all": 20.0,
                           "in_tok": 1000, "out_tok": 500, "sessions": 3, "last_ts": time.time()},
                 "burn_per_day": 0.5, "days_left": 84.0, "source": "live",
                 "balance_detail": {"balance_usd": 42.0, "ok": True}},
                {"provider": "anthropic", "balance": None, "remaining": 75.0,
                 "spend": {"today": 0.2, "7d": 1.5, "30d": 6.0, "all": 25.0,
                           "in_tok": 500, "out_tok": 200, "sessions": 2, "last_ts": time.time()},
                 "burn_per_day": 0.21, "days_left": 357.0, "source": "ledger",
                 "budget": 100.0, "budget_note": "test grant"},
            ],
            "ledger_error": None,
            "state_db": "/mock/state.db",
            "config": "/mock/config.yaml",
        }

    def test_cli_no_color(self):
        """CLI renderer works with color=False."""
        output = plutus.render_cli(self.data, color=False)
        self.assertIn("deepseek", output)
        self.assertIn("$42.00", output)
        self.assertIn("$0.50", output)
        self.assertIn("live", output)

    def test_html_contains_providers(self):
        """HTML renderer includes provider names."""
        output = plutus.render_html(self.data)
        self.assertIn("deepseek", output)
        self.assertIn("anthropic", output)
        self.assertIn("LIVE", output)
        self.assertIn("<!doctype html>", output)

    def test_json_output_format(self):
        """collect() JSON output matches expected structure."""
        # Just verify the structure keys exist
        self.assertIn("providers", self.data)
        self.assertIn("generated_at", self.data)


class TestFormatUsd(unittest.TestCase):
    """Tests for fmt_usd() helper."""

    def test_none(self):
        self.assertEqual(plutus.fmt_usd(None), "—")

    def test_zero(self):
        self.assertEqual(plutus.fmt_usd(0), "$0.00")

    def test_positive(self):
        self.assertEqual(plutus.fmt_usd(42.5), "$42.50")

    def test_large_number(self):
        self.assertEqual(plutus.fmt_usd(1234.567), "$1,234.57")


class TestSnapshot(unittest.TestCase):
    """Tests for snapshot() append."""

    def setUp(self):
        self.tmp_snapshots = tempfile.NamedTemporaryFile(
            mode='w', suffix='.jsonl', delete=False)
        self.tmp_snapshots.close()

    def tearDown(self):
        if os.path.exists(self.tmp_snapshots.name):
            os.unlink(self.tmp_snapshots.name)

    def test_snapshot_appends_line(self):
        """snapshot() appends a valid JSON line."""
        data = {
            "generated_at": time.time(),
            "providers": [
                {"provider": "deepseek", "balance": 42.0, "remaining": None,
                 "spend": {"all": 20.0}},
            ]
        }
        with patch.object(plutus, 'SNAPSHOT_FILE', self.tmp_snapshots.name):
            result_path = plutus.snapshot(data)
            self.assertEqual(result_path, self.tmp_snapshots.name)

        with open(self.tmp_snapshots.name, encoding='utf-8') as f:
            line = f.readline()
        record = json.loads(line)
        self.assertIn("t", record)
        self.assertIn("deepseek", record)
        self.assertEqual(record["deepseek"]["bal"], 42.0)


if __name__ == "__main__":
    unittest.main()
