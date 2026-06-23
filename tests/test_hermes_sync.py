#!/usr/bin/env python3
"""Tests for the Hermes → Plutus sync bridge (examples/hermes_sync.py)."""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "examples"))

import hermes_sync  # noqa: E402


def _make_db(rows, *, with_model=True):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    cols = ["billing_provider TEXT", "started_at REAL",
            "actual_cost_usd REAL", "estimated_cost_usd REAL",
            "input_tokens INT", "output_tokens INT",
            "cache_read_tokens INT", "reasoning_tokens INT"]
    if with_model:
        cols += ["model TEXT", "task_type TEXT"]
    conn.execute(f"CREATE TABLE sessions ({', '.join(cols)})")
    for r in rows:
        keys = ", ".join(r.keys())
        ph = ", ".join("?" * len(r))
        conn.execute(f"INSERT INTO sessions ({keys}) VALUES ({ph})", tuple(r.values()))
    conn.commit()
    conn.close()
    return path


class TestCollectSessions(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_paths", []):
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(p + ext)
                except OSError:
                    pass

    def _db(self, *a, **k):
        p = _make_db(*a, **k)
        self._paths = getattr(self, "_paths", []) + [p]
        return p

    def test_maps_rows_to_events(self):
        db = self._db([
            {"billing_provider": "anthropic", "model": "claude-opus-4-8",
             "task_type": "code_review", "actual_cost_usd": 0.14,
             "estimated_cost_usd": 0.20, "input_tokens": 1200, "output_tokens": 800,
             "cache_read_tokens": 0, "reasoning_tokens": 0},
        ])
        pairs = hermes_sync.collect_sessions(db, 0, workspace="hermes")
        self.assertEqual(len(pairs), 1)
        rowid, ev = pairs[0]
        self.assertEqual(ev["provider"], "anthropic")
        self.assertEqual(ev["model"], "claude-opus-4-8")
        self.assertEqual(ev["task_type"], "code_review")
        self.assertEqual(ev["cost_usd"], 0.14)      # actual preferred over estimated
        self.assertEqual(ev["input_tokens"], 1200)
        self.assertEqual(ev["workspace"], "hermes")
        self.assertEqual(ev["source"], "hermes")

    def test_estimated_cost_fallback(self):
        db = self._db([
            {"billing_provider": "google", "actual_cost_usd": 0,
             "estimated_cost_usd": 0.5, "input_tokens": 10, "output_tokens": 5,
             "cache_read_tokens": 0, "reasoning_tokens": 0,
             "model": "gemini", "task_type": "chat"},
        ])
        _, ev = hermes_sync.collect_sessions(db, 0)[0]
        self.assertEqual(ev["cost_usd"], 0.5)

    def test_watermark_filters(self):
        db = self._db([
            {"billing_provider": "a", "actual_cost_usd": 1, "estimated_cost_usd": 0,
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
             "reasoning_tokens": 0, "model": "m", "task_type": "t"},
            {"billing_provider": "b", "actual_cost_usd": 2, "estimated_cost_usd": 0,
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
             "reasoning_tokens": 0, "model": "m", "task_type": "t"},
        ])
        first_rowid = hermes_sync.collect_sessions(db, 0)[0][0]
        after = hermes_sync.collect_sessions(db, first_rowid)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0][1]["provider"], "b")

    def test_tolerates_missing_model_and_task(self):
        db = self._db([
            {"billing_provider": "deepseek", "actual_cost_usd": 0.01,
             "estimated_cost_usd": 0, "input_tokens": 5, "output_tokens": 5,
             "cache_read_tokens": 0, "reasoning_tokens": 0},
        ], with_model=False)
        _, ev = hermes_sync.collect_sessions(db, 0)[0]
        self.assertNotIn("model", ev)         # column absent → omitted
        self.assertEqual(ev["task_type"], "agent")  # default


if __name__ == "__main__":
    unittest.main()
