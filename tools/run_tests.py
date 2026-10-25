#!/usr/bin/env python3
"""Portable unittest entry point; no hardware or external Python packages."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import allen_heath_dlive_full_mirroring as application

# Historical fixtures import the former module name.
sys.modules["mirror_app"] = application

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    print("Running", suite.countTestCases(), "loopback tests.", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
