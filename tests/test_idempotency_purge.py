#!/usr/bin/env python3
"""#80 (review F3): an orphaned in-flight Idempotency-Key auto-reclaims after a
grace window (so a crash can't 409 it forever), and a purge sweeper bounds the
table. A *completed* claim is never reclaimed, preserving replay."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db


class TestIdempotencyReclaim(unittest.TestCase):
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

    def test_fresh_inflight_is_not_reclaimed(self):
        self.assertTrue(db.claim_idempotency_key(self.conn, self.org, "k1"))
        # Immediate retry: still in flight (status NULL), must NOT re-claim.
        self.assertFalse(db.claim_idempotency_key(self.conn, self.org, "k1"))

    def test_orphaned_inflight_reclaims_after_grace(self):
        # Simulate a claim that happened longer ago than the grace window and
        # never stored a response (process crashed).
        old = time.time() - db.IDEMPOTENCY_INFLIGHT_GRACE - 5
        db.claim_idempotency_key(self.conn, self.org, "k2", ts=old)
        # A new request with the same key reclaims it (returns True = re-process).
        self.assertTrue(db.claim_idempotency_key(self.conn, self.org, "k2"))

    def test_completed_claim_never_reclaimed(self):
        old = time.time() - db.IDEMPOTENCY_INFLIGHT_GRACE - 5
        db.claim_idempotency_key(self.conn, self.org, "k3", ts=old)
        db.store_idempotency_response(self.conn, self.org, "k3", 200, '{"ok":true}')
        # Even though it's old, it has a stored response → replay, not re-claim.
        self.assertFalse(db.claim_idempotency_key(self.conn, self.org, "k3"))
        self.assertEqual(db.idempotency_response(self.conn, self.org, "k3")[0], 200)

    def test_purge_drops_old_rows(self):
        old = time.time() - 100_000
        db.claim_idempotency_key(self.conn, self.org, "old", ts=old)
        db.claim_idempotency_key(self.conn, self.org, "new")
        removed = db.purge_idempotency(self.conn, older_than_seconds=86400)
        self.assertEqual(removed, 1)
        self.assertIsNone(db.idempotency_response(self.conn, self.org, "old"))
        self.assertIsNotNone(db.idempotency_response(self.conn, self.org, "new"))


if __name__ == "__main__":
    unittest.main()
