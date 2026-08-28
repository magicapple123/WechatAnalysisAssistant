"""Fail-closed checks for native libraries in the public Windows package.

This is deliberately separate from the private-data release scan.  A clean
security scan does not establish redistribution rights for a native binary.
See ``THIRD_PARTY_NOTICES.md`` for the evidence behind this policy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence


DEFAULT_POLICY_PATH = Path(__file__).with_name("public_native_runtime.json")


@dataclass(frozen=True)
class BlockedLibrary:
    pattern: re.Pattern[str]
    component: str
    reason: str


@dataclass(frozen=True)
class Finding:
    path: Path
    component: str
    reason: str


# The PyAV 18.0.0 Windows wheel contains x264/x265 built under GPL-2.0-or-
# later (or a separately purchased commercial licence), while its vendor build
# patches FFmpeg's licence classifier to report LGPLv3.  No commercial grant is
# present in the wheel or either upstream repository.  The wheel also copies
# several DLLs from an unpinned MSYS2 toolchain without recording the package
# versions or corresponding-source identity.  Public builds stay blocked until
# those binaries are replaced by a reproducible, reviewed runtime.
BLOCKED_LIBRARIES: tuple[BlockedLibrary, ...] = (
    BlockedLibrary(
        re.compile(r"^libx264(?:[-_.].*)?\.dll$", re.IGNORECASE),
        "x264",
        "GPL-2.0-or-later/commercial binary with no commercial grant or GPL-compliant release plan",
    ),
    BlockedLibrary(
        re.compile(r"^libx265(?:[-_.].*)?\.dll$", re.IGNORECASE),
        "x265",
        "GPL-2.0-or-later/commercial binary with no commercial grant or GPL-compliant release plan",
    ),
    BlockedLibrary(
        re.compile(r"^libgcc_s(?:[-_.].*)?\.dll$", re.IGNORECASE),
        "GCC runtime",
        "copyleft runtime copied from an unpinned MSYS2 toolchain; exact corresponding source is not recorded",
    ),
    BlockedLibrary(
        re.compile(r"^libstdc\+\+(?:[-_.].*)?\.dll$", re.IGNORECASE),
        "libstdc++",
        "copyleft runtime copied from an unpinned MSYS2 toolchain; exact corresponding source is not recorded",
    ),
    BlockedLibrary(
        re.compile(r"^libiconv(?:[-_.].*)?\.dll$", re.IGNORECASE),
        "GNU libiconv",
        "LGPL runtime copied from an unpinned MSYS2 toolchain; exact package/build provenance is not recorded",
    ),
    BlockedLibrary(
        re.compile(r"^libwinpthread(?:[-_.].*)?\.dll$", re.IGNORECASE),
        "MinGW-w64 winpthreads",
        "runtime copied from an unpinned MSYS2 toolchain without versioned licence/source provenance",
    ),
)


class PublicRedistributionError(RuntimeError):
    """Raised when a native runtime is not approved for public distribution."""


def installed_pyav_libs() -> Path:
    """Return the sibling ``av.libs`` directory for the selected interpreter."""

    spec = importlib.util.find_spec("av")
    if spec is None or spec.origin is None:
        raise PublicRedistributionError(
            "PyAV is not installed in the packaging interpreter; cannot audit its native runtime."
        )
    wheel_libs = Path(spec.origin).resolve().parent.parent / "av.libs"
    if not wheel_libs.is_dir():
        raise PublicRedistributionError(
            f"PyAV native library directory is missing: {wheel_libs}"
        )
    return wheel_libs


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_approved_bundles(policy_path: Path) -> list[dict[str, object]]:
    policy_path = policy_path.resolve()
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicRedistributionError(
            f"Native redistribution policy is missing or invalid: {policy_path}: {exc}"
        ) from exc

    if policy.get("schema_version") != 1:
        raise PublicRedistributionError(
            f"Unsupported native redistribution policy schema: {policy_path}"
        )
    bundles = policy.get("approved_bundles")
    if not isinstance(bundles, list):
        raise PublicRedistributionError(
            f"Native redistribution policy has no approved_bundles list: {policy_path}"
        )
    return bundles


def _approved_bundle_name(
    bundle_dir: Path,
    policy_path: Path,
    approved_bundles: Sequence[dict[str, object]],
) -> str | None:
    observed_files = {
        path.name.lower(): _sha256(path)
        for path in bundle_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".dll"
    }
    project_root = policy_path.resolve().parent.parent

    for bundle in approved_bundles:
        name = bundle.get("name")
        expected_files = bundle.get("files")
        required_licenses = bundle.get("required_license_files")
        if (
            not isinstance(name, str)
            or not isinstance(expected_files, dict)
            or not isinstance(required_licenses, dict)
        ):
            raise PublicRedistributionError(
                f"Malformed approved bundle entry in {policy_path}"
            )
        normalized_expected = {
            str(filename).lower(): str(digest).lower()
            for filename, digest in expected_files.items()
        }
        if observed_files != normalized_expected:
            continue

        licenses_match = True
        for relative_path, expected_digest in required_licenses.items():
            license_path = project_root / str(relative_path)
            if (
                not license_path.is_file()
                or _sha256(license_path) != str(expected_digest).lower()
            ):
                licenses_match = False
                break
        if licenses_match:
            return name
    return None


def scan_roots(
    roots: Iterable[Path],
    *,
    policy_path: Path = DEFAULT_POLICY_PATH,
) -> tuple[list[Finding], list[Path]]:
    """Scan roots and return blocked DLLs plus any FFmpeg codec DLLs seen."""

    approved_bundles = _load_approved_bundles(policy_path)
    findings: list[Finding] = []
    avcodec_libraries: list[Path] = []
    for raw_root in roots:
        root = Path(raw_root).resolve()
        if not root.is_dir():
            raise PublicRedistributionError(
                f"Redistribution audit root is missing or is not a directory: {root}"
            )
        try:
            dlls = sorted(
                (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".dll"),
                key=lambda path: str(path).lower(),
            )
        except OSError as exc:
            raise PublicRedistributionError(
                f"Unable to enumerate native libraries below {root}: {exc}"
            ) from exc

        for dll in dlls:
            if re.match(r"^avcodec(?:[-_.].*)?\.dll$", dll.name, re.IGNORECASE):
                avcodec_libraries.append(dll)
            for policy in BLOCKED_LIBRARIES:
                if policy.pattern.match(dll.name):
                    findings.append(Finding(dll, policy.component, policy.reason))
                    break

    for bundle_dir in sorted({path.parent for path in avcodec_libraries}):
        if _approved_bundle_name(bundle_dir, policy_path, approved_bundles) is None:
            findings.append(
                Finding(
                    next(path for path in avcodec_libraries if path.parent == bundle_dir),
                    "FFmpeg/PyAV native bundle",
                    "the exact DLL set, SHA-256 hashes, and required licence files are not in the reviewed allowlist",
                )
            )
    return findings, avcodec_libraries


def assert_public_redistribution_ready(
    roots: Sequence[Path],
    *,
    policy_path: Path = DEFAULT_POLICY_PATH,
) -> list[Path]:
    """Raise with an actionable report unless every scanned DLL is approved."""

    findings, avcodec_libraries = scan_roots(roots, policy_path=policy_path)
    if not findings:
        return avcodec_libraries

    lines = [
        "Public Windows packaging is BLOCKED by unresolved native-library redistribution obligations:",
    ]
    lines.extend(
        f"  - {finding.path}: {finding.component} - {finding.reason}"
        for finding in findings
    )
    lines.extend(
        (
            "Notices alone do not cure these findings. Replace the PyAV/FFmpeg runtime with a",
            "reproducible build that omits x264/x265 and records every toolchain runtime, or",
            "document appropriate commercial/GPL redistribution rights and obtain legal review.",
            "See THIRD_PARTY_NOTICES.md for the audited provenance and remediation criteria.",
        )
    )
    raise PublicRedistributionError("\n".join(lines))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Block public packaging when native redistribution rights are unresolved."
    )
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="Frozen or staged directories to scan recursively.",
    )
    parser.add_argument(
        "--installed-pyav",
        action="store_true",
        help="Also scan av.libs used by this Python interpreter.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    roots = list(args.roots)
    try:
        if args.installed_pyav:
            roots.append(installed_pyav_libs())
        if not roots:
            raise PublicRedistributionError(
                "No audit root was supplied; use --installed-pyav or pass a frozen directory."
            )
        avcodec_libraries = assert_public_redistribution_ready(roots)
    except PublicRedistributionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    detail = (
        f"; inspected {len(avcodec_libraries)} FFmpeg avcodec DLL(s)"
        if avcodec_libraries
        else ""
    )
    print(f"Public redistribution check passed{detail}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
