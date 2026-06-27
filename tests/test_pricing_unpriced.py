#!/usr/bin/env python3
"""#64: estimates that fall back to a default are flagged `unpriced`, and the
price table covers current 2026 models with a dated stamp.

The worst failure mode for a billing engine is a silently-wrong estimate that
looks authoritative — so a fallback must be signalled, not hidden."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering, pricing


class TestResolvePrice(unittest.TestCase):
    def test_exact_known_model(self):
        _, exact = pricing.resolve_price("anthropic", "claude-opus-4-8")
        self.assertTrue(exact)

    def test_new_2026_models_are_priced(self):
        for prov, model in [("anthropic", "claude-fable-5"),
                            ("openai", "gpt-5"), ("google", "gemini-2.5-pro"),
                            ("xai", "grok-4"), ("mistral", "mistral-large-2")]:
            price, exact = pricing.resolve_price(prov, model)
            self.assertTrue(exact, f"{prov}/{model} should be exactly priced")
            self.assertGreater(price.output, 0)

    def test_unknown_model_under_known_provider_is_fallback(self):
        _, exact = pricing.resolve_price("anthropic", "claude-does-not-exist")
        self.assertFalse(exact)

    def test_unknown_provider_is_fallback(self):
        price, exact = pricing.resolve_price("acme-llm", "x")
        self.assertFalse(exact)
        self.assertGreater(price.input, 0)

    def test_missing_model_is_fallback(self):
        _, exact = pricing.resolve_price("anthropic", None)
        self.assertFalse(exact)

    def test_override_exact_match(self):
        ov = {"openai": {"gpt-5": {"input": 1.0, "output": 2.0}}}
        price, exact = pricing.resolve_price("openai", "gpt-5", ov)
        self.assertTrue(exact)
        self.assertEqual((price.input, price.output), (1.0, 2.0))

    def test_override_default_is_fallback(self):
        ov = {"openai": {"_default": {"input": 1.0, "output": 2.0}}}
        _, exact = pricing.resolve_price("openai", "some-new-model", ov)
        self.assertFalse(exact)

    def test_price_table_dated(self):
        self.assertRegex(pricing.PRICE_TABLE_AS_OF, r"^\d{4}-\d{2}-\d{2}$")


class TestUnpricedFlag(unittest.TestCase):
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

    def test_known_model_not_unpriced(self):
        res = metering.record_usage(self.conn, self.org, provider="anthropic",
                                    model="claude-opus-4-8",
                                    input_tokens=1000, output_tokens=1000)
        self.assertTrue(res.estimated)
        self.assertFalse(res.unpriced)

    def test_unknown_model_is_unpriced(self):
        res = metering.record_usage(self.conn, self.org, provider="anthropic",
                                    model="claude-mystery-9",
                                    input_tokens=1000, output_tokens=1000)
        self.assertTrue(res.estimated)
        self.assertTrue(res.unpriced)

    def test_exact_cost_is_never_unpriced(self):
        res = metering.record_usage(self.conn, self.org, provider="anthropic",
                                    model="claude-mystery-9", cost_usd=0.42)
        self.assertFalse(res.estimated)
        self.assertFalse(res.unpriced)


if __name__ == "__main__":
    unittest.main()
