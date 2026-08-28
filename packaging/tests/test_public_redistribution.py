from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


CHECKER_PATH = Path(__file__).resolve().parents[1] / "check_public_redistribution.py"
SPEC = importlib.util.spec_from_file_location("public_redistribution_check", CHECKER_PATH)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


class PublicRedistributionCheckTests(unittest.TestCase):
    def test_unknown_lgpl_ffmpeg_bundle_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            avcodec = root / "avcodec-62-example.dll"
            avcodec.touch()

            with self.assertRaises(checker.PublicRedistributionError) as context:
                checker.assert_public_redistribution_ready([root])

            self.assertIn("reviewed allowlist", str(context.exception))

    def test_exact_reviewed_bundle_and_license_hashes_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            bundle_root = project_root / "runtime"
            bundle_root.mkdir()
            avcodec = bundle_root / "avcodec-62-reviewed.dll"
            avcodec.write_bytes(b"reviewed ffmpeg binary")
            license_path = project_root / "packaging" / "licenses" / "FFmpeg.txt"
            license_path.parent.mkdir(parents=True)
            license_path.write_text("reviewed licence\n", encoding="utf-8")
            policy_path = project_root / "packaging" / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "approved_bundles": [
                            {
                                "name": "reviewed-test-bundle",
                                "files": {
                                    avcodec.name: hashlib.sha256(avcodec.read_bytes()).hexdigest()
                                },
                                "required_license_files": {
                                    "packaging/licenses/FFmpeg.txt": hashlib.sha256(
                                        license_path.read_bytes()
                                    ).hexdigest()
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            observed = checker.assert_public_redistribution_ready(
                [bundle_root], policy_path=policy_path
            )

            self.assertEqual(observed, [avcodec])

    def test_gpl_codecs_and_unpinned_toolchain_runtimes_are_blocked(self) -> None:
        names = (
            "libx264-165-example.dll",
            "LIBX265-example.DLL",
            "libgcc_s_seh-1-example.dll",
            "libstdc++-6-example.dll",
            "libiconv-2-example.dll",
            "libwinpthread-1-example.dll",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "_internal" / "av.libs"
            nested.mkdir(parents=True)
            for name in names:
                (nested / name).touch()

            with self.assertRaises(checker.PublicRedistributionError) as context:
                checker.assert_public_redistribution_ready([root])

            message = str(context.exception)
            for name in names:
                self.assertIn(name, message)
            self.assertIn("Notices alone do not cure", message)

    def test_missing_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with self.assertRaises(checker.PublicRedistributionError):
                checker.scan_roots([missing])


if __name__ == "__main__":
    unittest.main()
