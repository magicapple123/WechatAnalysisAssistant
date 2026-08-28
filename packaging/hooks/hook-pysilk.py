"""Collect both silk-python backends selected dynamically at runtime."""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


hiddenimports = collect_submodules("pysilk")
binaries = collect_dynamic_libs("pysilk")
