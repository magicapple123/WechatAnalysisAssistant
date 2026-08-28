"""Make wheel-bundled native libraries discoverable in the frozen sidecar.

PyAV's Windows wheel keeps FFmpeg in ``av.libs`` next to the ``av`` package.
The wheel normally registers that directory itself, but doing it here as well
makes the onedir layout explicit and avoids PATH-dependent behaviour.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


_dll_directory_handles: list[object] = []

if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for candidate in (bundle_root / "av.libs", bundle_root):
        if not candidate.is_dir():
            continue
        try:
            # Keep the handle alive for the lifetime of the process. Letting it
            # be garbage-collected removes the directory from the DLL search.
            _dll_directory_handles.append(os.add_dll_directory(str(candidate)))
        except OSError:
            pass
