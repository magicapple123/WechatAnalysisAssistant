# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build for the local FastAPI sidecar.

The Electron application owns ``frontend/dist``. This target deliberately
contains backend Python code and third-party runtime libraries only.
"""

import importlib.util
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import (
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


PROJECT_ROOT = Path(SPECPATH).resolve().parent
HOOKS_DIR = PROJECT_ROOT / "packaging" / "hooks"
RUNTIME_HOOK = PROJECT_ROOT / "packaging" / "runtime_hooks" / "windows_dll_paths.py"
REDISTRIBUTION_CHECK = PROJECT_ROOT / "packaging" / "check_public_redistribution.py"
THIRD_PARTY_NOTICES = PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"
NATIVE_LICENSES_DIR = PROJECT_ROOT / "packaging" / "licenses"


def enforce_public_redistribution_policy():
    """Stop a direct PyInstaller invocation before unapproved DLLs are frozen."""

    module_spec = importlib.util.spec_from_file_location(
        "wechat_public_redistribution_check", REDISTRIBUTION_CHECK
    )
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"Unable to load redistribution check: {REDISTRIBUTION_CHECK}")
    checker = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = checker
    module_spec.loader.exec_module(checker)
    try:
        checker.assert_public_redistribution_ready([checker.installed_pyav_libs()])
    except checker.PublicRedistributionError as exc:
        raise SystemExit(str(exc)) from exc


enforce_public_redistribution_policy()


def optional_submodules(package):
    try:
        return collect_submodules(package)
    except Exception:
        return []


def optional_dynamic_libs(package):
    try:
        return collect_dynamic_libs(package)
    except Exception:
        return []


def optional_metadata(distribution):
    try:
        return copy_metadata(distribution)
    except Exception:
        return []


# Imports used behind try/except blocks or inside request handlers are invisible
# to static analysis. Keep this list aligned with packaging/DEPENDENCY_AUDIT.md.
INCLUDE_WX_KEY = os.environ.get("WECHAT_ASSISTANT_INCLUDE_WX_KEY_BUILD") == "1"

RUNTIME_PACKAGES = (
    "av",
    "PIL",
    "pillow_heif",
    "Crypto",
    "pymem",
    "pysilk",
    "zstandard",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_core",
    "uvicorn",
) + (("wx_key",) if INCLUDE_WX_KEY else ())

hiddenimports = [
    "backend.api",
    "_pillow_heif",
    "_cffi_backend",
    "yara",
    "pefile",
    "psutil",
    "aiofiles",
    "multipart",
    "httptools",
    "websockets",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "pythoncom",
    "pywintypes",
]
for package in RUNTIME_PACKAGES:
    hiddenimports += optional_submodules(package)

binaries = []
for package in ("av", "pillow_heif", "pysilk", "zstandard", "Crypto"):
    binaries += optional_dynamic_libs(package)

datas = [
    (str(THIRD_PARTY_NOTICES), "."),
    (str(NATIVE_LICENSES_DIR), "THIRD_PARTY_LICENSES/native"),
]
for distribution in (
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "Pillow",
    "pillow-heif",
    # PyAV's wheel currently places an executable AUTHORS.py plus a compiled
    # __pycache__ below ``av-*.dist-info/licenses``.  PyAV exposes its version
    # from the extension package and does not need distribution metadata at
    # runtime, so omitting this metadata keeps the release free of source/cache
    # artifacts without weakening the safety scan.
    "pycryptodome",
    "pymem",
    "silk-python",
    "zstandard",
    "yara-python",
    "pywin32",
):
    datas += optional_metadata(distribution)

a = Analysis(
    [str(PROJECT_ROOT / "backend" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[str(HOOKS_DIR)],
    hooksconfig={},
    runtime_hooks=[str(RUNTIME_HOOK)],
    excludes=[
        "backend.tests",
        "faster_whisper",
        "torch",
        "tensorflow",
        "pytest",
    ] + ([] if INCLUDE_WX_KEY else ["wx_key"]),
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WechatAnalysisAssistantBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Electron starts the process with windowsHide=True. Keep the console
    # subsystem so stdout/stderr pipes remain usable without showing a window.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WechatAnalysisAssistantBackend",
)
