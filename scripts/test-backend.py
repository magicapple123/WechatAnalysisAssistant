"""Run backend tests without ever touching the user's application data."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wechat-assistant-tests-") as temporary:
        os.environ["WECHAT_ASSISTANT_DATA_DIR"] = temporary
        backend_suite = unittest.TestLoader().discover(
            str(ROOT / "backend" / "tests"),
            pattern="test_*.py",
            top_level_dir=str(ROOT),
        )
        # Packaging policy is part of the public release boundary, not an
        # optional maintainer check.  Keep its standalone tests in the normal
        # backend/CI entry point without making ``packaging`` a Python package
        # (which would shadow the third-party package of the same name).
        packaging_suite = unittest.TestLoader().discover(
            str(ROOT / "packaging" / "tests"),
            pattern="test_*.py",
        )
        suite = unittest.TestSuite((backend_suite, packaging_suite))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
