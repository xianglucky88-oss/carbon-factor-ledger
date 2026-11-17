"""Convenience entry point for the independent QA layer.

Usage:
    python run_qa.py                 # full QA suite
    python run_qa.py -v              # verbose
    QA_REGEN_SNAPSHOTS=1 python run_qa.py   # regenerate golden snapshots

Equivalent unittest invocation:
    python -m unittest discover -s qa -t .
"""

import sys
import unittest

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir="qa", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
