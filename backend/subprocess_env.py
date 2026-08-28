"""Environment handling for backend child processes.

Desktop session metadata is an implementation detail of the local sidecar and
must not be inherited by unrelated programs such as WeChat, PowerShell, or
Windows command-line utilities.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


DESKTOP_ONLY_ENV_VARS = frozenset(
    {
        "WECHAT_ASSISTANT_DESKTOP_TOKEN",
        "WECHAT_ASSISTANT_DESKTOP_PORT",
        "WECHAT_ASSISTANT_DESKTOP_MODE",
        "WECHAT_ASSISTANT_PARENT_PID",
        "WECHAT_ASSISTANT_APP_VERSION",
        "WECHAT_ASSISTANT_DESKTOP_API_VERSION",
        "WECHAT_ASSISTANT_DESKTOP_USER_DATA_DIR",
        "WECHAT_ASSISTANT_SMOKE_TEST",
        "WECHAT_ASSISTANT_SMOKE_TIMEOUT_MS",
    }
)


def sanitized_subprocess_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child-process environment without desktop session metadata.

    Windows environment variable names are case-insensitive, so removal is
    deliberately case-insensitive even when a caller supplies a plain mapping.
    The input mapping is never mutated.
    """

    source = os.environ if environ is None else environ
    return {
        key: value
        for key, value in source.items()
        if key.upper() not in DESKTOP_ONLY_ENV_VARS
    }
