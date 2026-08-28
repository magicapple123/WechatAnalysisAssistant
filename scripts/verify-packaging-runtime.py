"""Fail fast when the build environment lacks a packaged runtime component."""

from __future__ import annotations

import importlib
import importlib.metadata
import sys


REQUIRED_MODULES = (
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "httptools",
    "websockets",
    "aiofiles",
    "multipart",
    "psutil",
    "pefile",
    "yara",
    "pymem",
    "Crypto.Cipher.AES",
    "PIL.Image",
    "pillow_heif",
    "_pillow_heif",
    "av",
    "pysilk",
    "pysilk.backends.cython._silk",
    "zstandard",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "pythoncom",
    "pywintypes",
)

# wx_key is an unlicensed third-party native enhancement and therefore cannot
# be redistributed by the open-source project. When a maintainer supplies a
# compatible local wheel, the spec still collects and probes it; the public
# build otherwise keeps the PyMem/manual-key fallback paths.
OPTIONAL_MODULES = ("wx_key",)


def main() -> int:
    failures: list[str] = []
    optional_failures: list[str] = []
    loaded = {}
    for name in REQUIRED_MODULES:
        try:
            loaded[name] = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report every packaging failure.
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    for name in OPTIONAL_MODULES:
        try:
            loaded[name] = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - optional native component.
            optional_failures.append(f"{name}: {type(exc).__name__}: {exc}")

    av = loaded.get("av")
    if av is not None:
        try:
            av.CodecContext.create("hevc", "r")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"PyAV HEVC/FFmpeg: {type(exc).__name__}: {exc}")

    pillow_heif = loaded.get("pillow_heif")
    if pillow_heif is not None:
        try:
            pillow_heif.register_heif_opener()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"pillow-heif/libheif: {type(exc).__name__}: {exc}")

    if failures:
        print("Packaging runtime verification failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    for failure in optional_failures:
        print(
            f"Optional packaging component unavailable ({failure}); "
            "building with the supported fallback path.",
            file=sys.stderr,
        )

    versions = []
    for distribution in (
        "fastapi",
        "uvicorn",
        "Pillow",
        "pillow-heif",
        "av",
        "silk-python",
        "zstandard",
        "pywin32",
        "wx_key",
    ):
        try:
            versions.append(f"{distribution}={importlib.metadata.version(distribution)}")
        except importlib.metadata.PackageNotFoundError:
            pass
    print("Packaging runtime verification passed: " + ", ".join(versions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
