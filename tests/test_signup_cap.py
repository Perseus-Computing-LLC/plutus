#!/usr/bin/env python3
"""#33: DB-backed per-day cap on self-serve org creation (survives restarts),
alongside the in-memory hourly limiter."""
import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import auth as authmod


def _cfg(**over):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["auth"].update({
        "enabled": True,
        "google_client_id": "cid",
        "google_client_secret": "secret",
        "base_url": "http://127.0.0.1",
        "allow_signup": True,
    })
    cfg["auth"].update(over)
    return cfg


class TestSignupDailyCap(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        authmod._signup_times.clear()  # isolate the in-memory hourly limiter

    def tearDown(self):
        self.conn.close()
        authmod._signup_times.clear()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_cap_reached_blocks_signup(self):
        # cap=2, and two orgs already exist in the last 24h → next signup blocked
        db.create_org(self.conn, "Existing A")
        db.create_org(self.conn, "Existing B")
        with self.assertRaises(authmod.AuthError) as cm:
            authmod._authorize_email(self.conn, _cfg(max_new_orgs_per_day=2),
                                     "new@stranger.com")
        self.assertIn("daily", str(cm.exception).lower())

    def test_under_cap_allows_signup(self):
        uid = authmod._authorize_email(self.conn, _cfg(max_new_orgs_per_day=50),
                                       "ok@stranger.com", name="OK")
        self.assertIsNotNone(uid)

    def test_cap_zero_disables_daily_limit(self):
        # 0 == no daily cap; signup still works even with many existing orgs
        for i in range(5):
            db.create_org(self.conn, f"Org {i}")
        uid = authmod._authorize_email(self.conn, _cfg(max_new_orgs_per_day=0),
                                       "fresh@stranger.com")
        self.assertIsNotNone(uid)

    def test_count_helper_counts_recent_only(self):
        import time
        old = db.create_org(self.conn, "Old")["id"]
        # backdate one org beyond the 24h window
        self.conn.execute("UPDATE organizations SET created_at=? WHERE id=?",
                          (time.time() - 90000, old))
        self.conn.commit()
        db.create_org(self.conn, "Recent")
        self.assertEqual(db.count_orgs_created_since(self.conn, time.time() - 86400), 1)


if __name__ == "__main__":
    unittest.main()
