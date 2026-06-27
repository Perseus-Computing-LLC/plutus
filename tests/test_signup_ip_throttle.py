#!/usr/bin/env python3
"""#59: per-IP signup throttle so one source can't exhaust the global daily
signup budget (a self-DoS of the funnel)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db
from plutus_agent.server import auth


class TestPerIpLimiter(unittest.TestCase):
    def setUp(self):
        auth._signup_times_by_ip.clear()

    def test_allows_up_to_cap_then_blocks(self):
        ip = "203.0.113.7"
        self.assertTrue(auth._allow_signup_ip(ip, per_day=2))
        self.assertTrue(auth._allow_signup_ip(ip, per_day=2))
        self.assertFalse(auth._allow_signup_ip(ip, per_day=2))  # 3rd over cap

    def test_distinct_ips_independent(self):
        self.assertTrue(auth._allow_signup_ip("a", per_day=1))
        self.assertFalse(auth._allow_signup_ip("a", per_day=1))
        self.assertTrue(auth._allow_signup_ip("b", per_day=1))  # different IP ok

    def test_disabled_when_zero(self):
        for _ in range(10):
            self.assertTrue(auth._allow_signup_ip("x", per_day=0))


class TestAuthorizeEmailRespectsIpCap(unittest.TestCase):
    def setUp(self):
        auth._signup_times_by_ip.clear()
        auth._signup_times.clear()
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.dbpath)
        db.init_schema(self.conn)
        self.cfg = {"auth": {"allow_signup": True,
                             "max_signups_per_ip_per_day": 1,
                             "max_new_orgs_per_day": 0}}

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_first_signup_from_ip_succeeds_second_blocked(self):
        uid = auth._authorize_email(self.conn, self.cfg, "a@example.com",
                                    client_ip="198.51.100.1")
        self.assertTrue(uid)
        with self.assertRaises(auth.AuthError):
            auth._authorize_email(self.conn, self.cfg, "b@example.com",
                                  client_ip="198.51.100.1")

    def test_existing_member_not_subject_to_ip_cap(self):
        # First signup consumes the IP budget; that same user signing in again
        # is an existing member and must not be throttled.
        auth._authorize_email(self.conn, self.cfg, "a@example.com",
                              client_ip="198.51.100.2")
        uid = auth._authorize_email(self.conn, self.cfg, "a@example.com",
                                    client_ip="198.51.100.2")
        self.assertTrue(uid)


if __name__ == "__main__":
    unittest.main()
