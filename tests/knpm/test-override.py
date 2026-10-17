#!/usr/bin/env python3
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

HARNESS_PATH = Path(__file__).with_name("run-tests.py")
SPEC = importlib.util.spec_from_file_location("knpm_harness", HARNESS_PATH)

if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load {HARNESS_PATH}")

harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)

SHARED = "usr/share/knpm-override-test/shared.txt"
A_ONLY = "usr/share/knpm-override-test/owner-a-only.txt"
B_ONLY = "usr/share/knpm-override-test/owner-b-only.txt"
BLOCKED = "usr/share/knpm-override-z/blocked.txt"

A_CONTENT = b"original owner A content\n"
B_CONTENT = b"replacement owner B content\n"
A_ONLY_CONTENT = b"file retained by owner A\n"
B_ONLY_CONTENT = b"file belonging only to owner B\n"


def directory(mode=0o755):
    return ("D", mode, None)


def file_entry(content, mode=0o644):
    return ("F", mode, content)


def common_directories():
    return {
        "usr": directory(),
        "usr/share": directory(),
        "usr/share/knpm-override-test": directory(),
    }


def owner_a_payload():
    payload = common_directories()
    payload[SHARED] = file_entry(A_CONTENT)
    payload[A_ONLY] = file_entry(A_ONLY_CONTENT)
    return payload


def owner_b_payload():
    payload = common_directories()
    payload[SHARED] = file_entry(B_CONTENT)
    payload[B_ONLY] = file_entry(B_ONLY_CONTENT)
    return payload


def failing_owner_b_payload():
    payload = common_directories()
    payload[SHARED] = file_entry(B_CONTENT)
    payload["usr/share/knpm-override-z"] = directory()
    payload[BLOCKED] = file_entry(b"This installation must fail\n")
    return payload


class KNPMOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="knpm-override-suite-",
            dir=harness.TESTS,
        )
        self.work = Path(self.temp.name)
        self.root = self.work / "root"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_package(self, name, payload):
        return harness.make_package(
            self.work / f"{name}.khmpkg",
            name,
            [],
            payload,
        )

    def record(self, name):
        return self.root / "var/lib/knpm/installed" / name

    def run_knpm(self, *args):
        return harness.run_knpm(*args, root=self.root)

    def assert_passes(self, result, description):
        self.assertEqual(
            result.returncode,
            0,
            f"{description} failed.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def assert_fails(self, result, description):
        self.assertNotEqual(
            result.returncode,
            0,
            f"{description} unexpectedly succeeded.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_01_override_requires_flag(self):
        package_a = self.make_package("override-owner-a", owner_a_payload())
        package_b = self.make_package("override-owner-b", owner_b_payload())

        self.assert_passes(
            self.run_knpm("install", package_a),
            "initial installation of owner A",
        )

        self.assert_fails(
            self.run_knpm("install", package_b),
            "conflicting installation without --override",
        )

        self.assertEqual((self.root / SHARED).read_bytes(), A_CONTENT)
        self.assertTrue(self.record("override-owner-a").is_dir())
        self.assertFalse(self.record("override-owner-b").exists())
        self.assertFalse((self.root / B_ONLY).exists())
        self.assertFalse((self.root / "var/lib/knpm/lock").exists())

    def test_02_override_transfers_only_conflicting_file(self):
        package_a = self.make_package("override-owner-a", owner_a_payload())
        package_b = self.make_package("override-owner-b", owner_b_payload())

        self.assert_passes(
            self.run_knpm("install", package_a),
            "initial installation of owner A",
        )
        self.assert_passes(
            self.run_knpm("install", "--override", package_b),
            "installation of owner B with --override",
        )

        self.assertEqual((self.root / SHARED).read_bytes(), B_CONTENT)
        self.assertEqual((self.root / A_ONLY).read_bytes(), A_ONLY_CONTENT)
        self.assertEqual((self.root / B_ONLY).read_bytes(), B_ONLY_CONTENT)

        manifest_a = (self.record("override-owner-a") / "manifest").read_text()
        manifest_b = (self.record("override-owner-b") / "manifest").read_text()

        self.assertIn(A_ONLY, manifest_a)
        self.assertNotIn(SHARED, manifest_a)
        self.assertIn(SHARED, manifest_b)
        self.assertIn(B_ONLY, manifest_b)

        listing = self.run_knpm("list")
        self.assert_passes(listing, "listing after override")
        self.assertIn("override-owner-a", listing.stdout)
        self.assertIn("override-owner-b", listing.stdout)

        # Removing the previous owner must not delete B's transferred file.
        self.assert_passes(
            self.run_knpm("remove", "override-owner-a"),
            "removal of previous owner",
        )
        self.assertFalse((self.root / A_ONLY).exists())
        self.assertEqual((self.root / SHARED).read_bytes(), B_CONTENT)
        self.assertTrue((self.root / B_ONLY).is_file())

        self.assert_passes(
            self.run_knpm("remove", "override-owner-b"),
            "removal of replacement owner",
        )
        self.assertFalse((self.root / SHARED).exists())
        self.assertFalse((self.root / B_ONLY).exists())

    def test_03_override_flag_after_package(self):
        package_a = self.make_package("override-owner-a", owner_a_payload())
        package_b = self.make_package("override-owner-b", owner_b_payload())

        self.assert_passes(
            self.run_knpm("install", package_a),
            "initial installation of owner A",
        )

        self.assert_passes(
            self.run_knpm("install", package_b, "--override"),
            "installation with --override after the package argument",
        )

        self.assertEqual((self.root / SHARED).read_bytes(), B_CONTENT)

        manifest_a = (self.record("override-owner-a") / "manifest").read_text()
        manifest_b = (self.record("override-owner-b") / "manifest").read_text()

        self.assertNotIn(SHARED, manifest_a)
        self.assertIn(A_ONLY, manifest_a)
        self.assertIn(SHARED, manifest_b)
        self.assertIn(B_ONLY, manifest_b)

    def test_03_failed_override_restores_original_owner(self):
        if os.geteuid() == 0:
            self.skipTest("Permission-based rollback test requires an unprivileged user")

        package_a = self.make_package("override-owner-a", owner_a_payload())
        package_b = self.make_package(
            "override-owner-b",
            failing_owner_b_payload(),
        )

        self.assert_passes(
            self.run_knpm("install", package_a),
            "initial installation of owner A",
        )

        record_a = self.record("override-owner-a")
        original_manifest = (record_a / "manifest").read_bytes()

        # Force a later file installation to fail without root privileges.
        blocked_dir = self.root / "usr/share/knpm-override-z"
        blocked_dir.mkdir(parents=True)
        blocked_dir.chmod(0o555)

        try:
            result = self.run_knpm(
                "install",
                "--override",
                package_b,
            )
        finally:
            blocked_dir.chmod(0o755)

        self.assert_fails(result, "override expected to fail in read-only directory")

        # Original content and owner database record must remain intact.
        self.assertEqual((self.root / SHARED).read_bytes(), A_CONTENT)
        self.assertEqual((self.root / A_ONLY).read_bytes(), A_ONLY_CONTENT)
        self.assertFalse((self.root / BLOCKED).exists())

        self.assertTrue(record_a.is_dir())
        restored_manifest = (record_a / "manifest").read_bytes()
        self.assertEqual(restored_manifest, original_manifest)
        self.assertIn(SHARED, restored_manifest.decode())
        self.assertIn(A_ONLY, restored_manifest.decode())

        self.assertFalse(self.record("override-owner-b").exists())
        self.assertFalse((self.root / "var/lib/knpm/lock").exists())

        listing = self.run_knpm("list")
        self.assert_passes(listing, "listing after failed override")
        self.assertIn("override-owner-a", listing.stdout)
        self.assertNotIn("override-owner-b", listing.stdout)

        # The restored ownership record must still work for normal removal.
        self.assert_passes(
            self.run_knpm("remove", "override-owner-a"),
            "removal after rollback",
        )
        self.assertFalse((self.root / SHARED).exists())
        self.assertFalse((self.root / A_ONLY).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
