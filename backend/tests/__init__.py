"""Backend regression tests with isolated application storage."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile


_TEST_DATA_DIRECTORY = None
if not os.environ.get("WECHAT_ASSISTANT_DATA_DIR"):
    _TEST_DATA_DIRECTORY = tempfile.mkdtemp(prefix="wechat-assistant-tests-")
    os.environ["WECHAT_ASSISTANT_DATA_DIR"] = _TEST_DATA_DIRECTORY
    atexit.register(shutil.rmtree, _TEST_DATA_DIRECTORY, ignore_errors=True)
