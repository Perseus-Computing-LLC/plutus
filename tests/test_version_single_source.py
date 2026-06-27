#!/usr/bin/env python3
"""#57: the package version is single-sourced from plutus_agent.__version__, so
pyproject metadata and `plutus version` can't drift apart."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plutus_agent

_PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "pyproject.toml")


class TestVersionSingleSource(unittest.TestCase):
    def setUp(self):
        with open(_PYPROJECT, encoding="utf-8") as f:
            self.text = f.read()

    def test_pyproject_declares_version_dynamic(self):
        self.assertRegex(self.text, r'dynamic\s*=\s*\[[^\]]*"version"')

    def test_pyproject_has_no_static_version(self):
        # No `version = "x.y.z"` inside [project] — that would shadow the dynamic
        # source and reintroduce drift.
        self.assertNotRegex(self.text, r'(?m)^\s*version\s*=\s*"\d')

    def test_dynamic_source_points_at_dunder_version(self):
        self.assertIn('attr = "plutus_agent.__version__"', self.text)

    def test_dunder_version_is_resolvable(self):
        self.assertRegex(plutus_agent.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
