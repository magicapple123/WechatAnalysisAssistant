"""Collect pillow-heif's top-level extension module and libheif DLL."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, get_package_paths


hiddenimports = collect_submodules("pillow_heif") + ["_pillow_heif"]
binaries = []

_, _package_dir = get_package_paths("pillow_heif")
_site_packages = Path(_package_dir).parent
for _pattern in ("_pillow_heif*.pyd", "libheif*.dll"):
    binaries += [
        (str(path), ".") for path in sorted(_site_packages.glob(_pattern))
    ]
