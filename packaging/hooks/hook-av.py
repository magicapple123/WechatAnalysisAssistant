"""PyInstaller hook for PyAV and the FFmpeg DLLs bundled by its wheel."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, get_package_paths


hiddenimports = collect_submodules("av")
binaries = []

_, _package_dir = get_package_paths("av")
_wheel_libs = Path(_package_dir).parent / "av.libs"
if _wheel_libs.is_dir():
    binaries += [
        (str(path), "av.libs") for path in sorted(_wheel_libs.glob("*.dll"))
    ]
