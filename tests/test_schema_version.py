#!/usr/bin/env python3
"""#67: schema-version stamping, the reader, and the forward-incompat guard."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db


class TestSchemaVersion(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_fresh_db_stamped_with_current_version(self):
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        self.assertEqual(db.get_schema_version(conn), db.SCHEMA_VERSION)
        conn.close()

    def test_reader_none_for_uninitialized_db(self):
        conn = db.connect(self.dbpath)
        # No meta table yet.
        self.assertIsNone(db.get_schema_version(conn))
        conn.close()

    def test_reinit_is_idempotent(self):
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        db.init_schema(conn)  # must not raise
        self.assertEqual(db.get_schema_version(conn), db.SCHEMA_VERSION)
        conn.close()

    def test_refuses_db_from_newer_plutus(self):
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        # Simulate a database written by a future Plutus.
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                     (str(db.SCHEMA_VERSION + 1),))
        conn.commit()
        with self.assertRaises(RuntimeError):
            db.init_schema(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
