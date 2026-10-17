#!/usr/bin/env python3
import filecmp
import gzip
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

KBUILD = Path(__file__).resolve().parents[2]
KNPM = Path.home() / "Projects/KhaymOS/knpm/target/release/knpm"
BASH_PACKAGE = KBUILD / "packages/bash-target-5.3.khmpkg"
STAGED_BASH = KBUILD / "destdir/bash-target-5.3/usr/bin/bash"
GLIBC_PACKAGE = KBUILD / "packages/glibc-2.44.khmpkg"
NCURSES_PACKAGE = KBUILD / "packages/ncurses-target-6.6.khmpkg"
READLINE_PACKAGE = KBUILD / "packages/readline-target-8.3.khmpkg"
TESTS = KBUILD / "tests/knpm"


def run_knpm(*args, root=None):
    env = os.environ.copy()
    if root is not None:
        env["KNPM_ROOT"] = str(root)

    return subprocess.run(
        [str(KNPM), *(str(arg) for arg in args)],
        env=env,
        capture_output=True,
        text=True,
    )


def add_directory(archive, name, mode=0o755):
    info = tarfile.TarInfo(name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    archive.addfile(info)


def add_file(archive, name, content, mode=0o644):
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = len(content)
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    archive.addfile(info, io.BytesIO(content))


def make_package(destination, name, depends, payload):
    """Build a minimal KHMPKG-1 package for isolated tests."""
    lines = []

    for path in sorted(payload, key=os.fsencode):
        kind, mode, content = payload[path]
        digest = "-" if kind == "D" else hashlib.sha256(content).hexdigest()
        lines.append(f"{kind}\t{mode:04o}\t{digest}\t{path}")

    metadata = (
        "format=KHMPKG-1\n"
        f"name={name}\n"
        "version=1.0\n"
        "arch=noarch\n"
        f"description=KNPM automated test package {name}\n"
        f"depends={','.join(depends)}\n"
    ).encode()

    manifest = ("\n".join(lines) + "\n").encode()

    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        add_directory(archive, "CONTROL")
        add_file(archive, "CONTROL/metadata", metadata)
        add_file(archive, "CONTROL/manifest", manifest)
        add_directory(archive, "data")

        for path in sorted(payload, key=os.fsencode):
            kind, mode, content = payload[path]
            archive_path = "data/" + path

            if kind == "D":
                add_directory(archive, archive_path, mode)
            else:
                add_file(archive, archive_path, content, mode)

    return destination


def sample_payload(filename, content):
    return {
        "usr": ("D", 0o755, None),
        "usr/share": ("D", 0o755, None),
        "usr/share/knpm-test": ("D", 0o755, None),
        f"usr/share/knpm-test/{filename}": ("F", 0o644, content),
    }


class KNPMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="knpm-suite-",
            dir=TESTS,
        )
        self.work = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def assert_command_fails(self, result, description):
        self.assertNotEqual(
            result.returncode,
            0,
            f"{description} unexpectedly succeeded.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

    def assert_command_passes(self, result, description):
        self.assertEqual(
            result.returncode,
            0,
            f"{description} failed.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

    def install_bash_dependencies(self, root):
        for package in (
            GLIBC_PACKAGE,
            NCURSES_PACKAGE,
            READLINE_PACKAGE,
        ):
            self.assert_command_passes(
                run_knpm("install", package, root=root),
                f"installation of dependency {package.name}",
            )

    def test_01_real_package_verify_and_lifecycle(self):
        self.assert_command_passes(
            run_knpm("verify", BASH_PACKAGE),
            "valid Bash package verification",
        )

        root = self.work / "root"
        root.mkdir()

        self.install_bash_dependencies(root)

        self.assert_command_passes(
            run_knpm("install", BASH_PACKAGE, root=root),
            "Bash installation",
        )

        installed = root / "usr/bin/bash"
        record = root / "var/lib/knpm/installed/bash-target"

        self.assertTrue(installed.is_file())
        self.assertTrue(filecmp.cmp(installed, STAGED_BASH, shallow=False))
        self.assertTrue(record.is_dir())

        listing = run_knpm("list", root=root)
        self.assert_command_passes(listing, "package listing")
        self.assertIn("bash-target", listing.stdout)

        self.assert_command_passes(
            run_knpm("remove", "bash-target", root=root),
            "Bash removal",
        )
        self.assertFalse(installed.exists())
        self.assertFalse(record.exists())

    def test_02_truncated_archive_rejected(self):
        truncated = self.work / "truncated.khmpkg"
        truncated.write_bytes(BASH_PACKAGE.read_bytes()[:-32])

        self.assert_command_fails(
            run_knpm("verify", truncated),
            "truncated archive verification",
        )

        root = self.work / "root"
        root.mkdir()

        self.assert_command_fails(
            run_knpm("install", truncated, root=root),
            "truncated archive installation",
        )

        self.assertFalse((root / "usr/bin/bash").exists())
        self.assertFalse(
            (root / "var/lib/knpm/installed/bash-target").exists()
        )

    def test_03_package_symlink_rejected(self):
        linked = self.work / "linked.khmpkg"
        linked.symlink_to(BASH_PACKAGE.resolve())

        self.assert_command_fails(
            run_knpm("verify", linked),
            "verification of package symlink",
        )

        root = self.work / "root"
        root.mkdir()

        self.assert_command_fails(
            run_knpm("install", linked, root=root),
            "installation from package symlink",
        )

        self.assertFalse((root / "usr/bin/bash").exists())
        self.assertFalse(
            (root / "var/lib/knpm/installed/bash-target").exists()
        )

    def test_04_existing_file_conflict_preserved(self):
        root = self.work / "root"
        binary = root / "usr/bin/bash"
        binary.parent.mkdir(parents=True)
        binary.write_text("DO-NOT-OVERWRITE\n")

        result = run_knpm("install", BASH_PACKAGE, root=root)
        self.assert_command_fails(result, "conflicting installation")

        self.assertEqual(binary.read_text(), "DO-NOT-OVERWRITE\n")
        self.assertFalse(
            (root / "var/lib/knpm/installed/bash-target").exists()
        )
        self.assertFalse((root / "var/lib/knpm/lock").exists())

    def test_05_modified_file_protected_on_removal(self):
        root = self.work / "root"
        root.mkdir()

        self.install_bash_dependencies(root)

        self.assert_command_passes(
            run_knpm("install", BASH_PACKAGE, root=root),
            "initial Bash installation",
        )

        binary = root / "usr/bin/bash"
        record = root / "var/lib/knpm/installed/bash-target"
        binary.write_text("USER MODIFICATION\n")

        result = run_knpm("remove", "bash-target", root=root)
        self.assert_command_fails(result, "removal of modified package")

        self.assertEqual(binary.read_text(), "USER MODIFICATION\n")
        self.assertTrue(record.is_dir())

        listing = run_knpm("list", root=root)
        self.assert_command_passes(listing, "listing after blocked removal")
        self.assertIn("bash-target", listing.stdout)

    def test_06_target_symlink_rejected(self):
        root = self.work / "root"
        outside = self.work / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "bin").mkdir()

        (root / "usr").symlink_to(outside, target_is_directory=True)

        self.assert_command_fails(
            run_knpm("install", BASH_PACKAGE, root=root),
            "installation through a target-root symlink",
        )

        self.assertFalse((outside / "bin/bash").exists())
        self.assertFalse(
            (root / "var/lib/knpm/installed/bash-target").exists()
        )
        self.assertFalse((root / "var/lib/knpm/lock").exists())

    def test_07_dependencies_and_shared_directories(self):
        payload_base = sample_payload(
            "base.txt",
            b"base package\n",
        )
        payload_app = sample_payload(
            "app.txt",
            b"dependent app\n",
        )

        base = make_package(
            self.work / "test-base.khmpkg",
            "test-base",
            [],
            payload_base,
        )
        app = make_package(
            self.work / "test-app.khmpkg",
            "test-app",
            ["test-base"],
            payload_app,
        )

        root = self.work / "root"
        root.mkdir()

        self.assert_command_passes(
            run_knpm("install", base, root=root),
            "base package installation",
        )
        self.assert_command_passes(
            run_knpm("install", app, root=root),
            "dependent app installation",
        )

        base_file = root / "usr/share/knpm-test/base.txt"
        app_file = root / "usr/share/knpm-test/app.txt"

        self.assertTrue(base_file.is_file())
        self.assertTrue(app_file.is_file())

        self.assert_command_fails(
            run_knpm("remove", "test-base", root=root),
            "removal of required base package",
        )
        self.assertTrue(base_file.is_file())
        self.assertTrue(app_file.is_file())

        self.assert_command_passes(
            run_knpm("remove", "test-app", root=root),
            "dependent app removal",
        )
        self.assert_command_passes(
            run_knpm("remove", "test-base", root=root),
            "base removal after app",
        )
        self.assertFalse(base_file.exists())
        self.assertFalse(app_file.exists())

    def test_08_install_rollback(self):
        if os.geteuid() == 0:
            self.skipTest(
                "permission-based rollback test must run as an unprivileged user"
            )

        payload = {
            "usr": ("D", 0o755, None),
            "usr/share": ("D", 0o755, None),
            "usr/share/knpm-rollback-a": ("D", 0o755, None),
            "usr/share/knpm-rollback-z": ("D", 0o755, None),
            "usr/share/knpm-rollback-a/a.txt":
                ("F", 0o644, b"first file\n"),
            "usr/share/knpm-rollback-z/b.txt":
                ("F", 0o644, b"second file\n"),
        }

        package = make_package(
            self.work / "rollback-test.khmpkg",
            "rollback-test",
            [],
            payload,
        )

        root = self.work / "root"
        blocked = root / "usr/share/knpm-rollback-z"
        blocked.mkdir(parents=True)
        blocked.chmod(0o555)

        try:
            self.assert_command_fails(
                run_knpm("install", package, root=root),
                "installation expected to fail on read-only directory",
            )
        finally:
            blocked.chmod(0o755)

        self.assertFalse(
            (root / "usr/share/knpm-rollback-a/a.txt").exists()
        )
        self.assertFalse(
            (root / "usr/share/knpm-rollback-z/b.txt").exists()
        )
        self.assertFalse(
            (root / "usr/share/knpm-rollback-a").exists()
        )
        self.assertFalse(
            (root / "var/lib/knpm/installed/rollback-test").exists()
        )
        self.assertFalse((root / "var/lib/knpm/lock").exists())


    def test_09_shared_directory_survives_creator_removal(self):
        creator_payload = {
            "usr": ("D", 0o755, None),
            "usr/share": ("D", 0o755, None),
            "usr/share/knpm-shared-empty": ("D", 0o755, None),
            "opt": ("D", 0o755, None),
            "opt/creator": ("D", 0o755, None),
            "opt/creator/data.txt": ("F", 0o644, b"creator package\n"),
        }

        consumer_payload = {
            "usr": ("D", 0o755, None),
            "usr/share": ("D", 0o755, None),
            "usr/share/knpm-shared-empty": ("D", 0o755, None),
            "var": ("D", 0o755, None),
            "var/lib": ("D", 0o755, None),
            "var/lib/knpm-consumer-marker.txt":
                ("F", 0o644, b"consumer remains installed\n"),
        }

        creator = make_package(
            self.work / "directory-creator.khmpkg",
            "directory-creator",
            [],
            creator_payload,
        )

        consumer = make_package(
            self.work / "directory-consumer.khmpkg",
            "directory-consumer",
            [],
            consumer_payload,
        )

        root = self.work / "root"
        root.mkdir()

        self.assert_command_passes(
            run_knpm("install", creator, root=root),
            "directory creator installation",
        )
        self.assert_command_passes(
            run_knpm("install", consumer, root=root),
            "directory consumer installation",
        )

        shared = root / "usr/share/knpm-shared-empty"
        self.assertTrue(shared.is_dir())

        self.assert_command_passes(
            run_knpm("remove", "directory-creator", root=root),
            "removal of directory creator",
        )

        # The creator is gone, but the consumer still owns this empty dir.
        self.assertTrue(
            shared.is_dir(),
            "removing the creator deleted a directory used by another package",
        )
        self.assertTrue(
            (root / "var/lib/knpm-consumer-marker.txt").is_file()
        )
        self.assertTrue(
            (root / "var/lib/knpm/installed/directory-consumer").is_dir()
        )


    def test_10_missing_dependency_rejected(self):
        package = make_package(
            self.work / "orphan-app.khmpkg",
            "orphan-app",
            ["package-that-is-not-installed"],
            sample_payload("app.txt", b"must not be installed\n"),
        )

        root = self.work / "root"
        root.mkdir()

        result = run_knpm("install", package, root=root)
        self.assert_command_fails(result, "installation with missing dependency")

        self.assertFalse((root / "usr/share/knpm-test/app.txt").exists())
        self.assertFalse(
            (root / "var/lib/knpm/installed/orphan-app").exists()
        )
        self.assertFalse((root / "var/lib/knpm/lock").exists())

    def test_11_archive_path_traversal_rejected(self):
        package = make_package(
            self.work / "traversal.khmpkg",
            "traversal-test",
            [],
            {
                "usr": ("D", 0o755, None),
                "usr/share": ("D", 0o755, None),
                "../escaped.txt": ("F", 0o644, b"must never escape\n"),
            },
        )

        root = self.work / "root"
        root.mkdir()

        result = run_knpm("verify", package)
        self.assert_command_fails(result, "verification of traversal path")

        result = run_knpm("install", package, root=root)
        self.assert_command_fails(result, "installation of traversal path")

        self.assertFalse((self.work / "escaped.txt").exists())
        self.assertFalse((root / "escaped.txt").exists())
        self.assertFalse((root / "usr/share/escaped.txt").exists())
        self.assertFalse(
            (root / "var/lib/knpm/installed/traversal-test").exists()
        )


    def test_12_oversized_compressed_package_rejected(self):
        oversized = self.work / "oversized.khmpkg"

        with oversized.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024 * 1024 + 1)

        self.assert_command_fails(
            run_knpm("verify", oversized),
            "oversized package verification",
        )

        root = self.work / "root"
        root.mkdir()

        self.assert_command_fails(
            run_knpm("install", oversized, root=root),
            "oversized package installation",
        )

        self.assertFalse((root / "usr/bin/bash").exists())
        self.assertFalse(
            (root / "var/lib/knpm/installed/bash-target").exists()
        )


    def test_13_uncompressed_payload_limit_rejected(self):
        oversized = self.work / "oversized-payload.khmpkg"

        def member(name, content=b"", kind=tarfile.REGTYPE,
                   mode=0o644, declared_size=None):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.size = len(content) if declared_size is None else declared_size

            header = info.tobuf(format=tarfile.GNU_FORMAT)
            padding = bytes((-len(content)) % tarfile.BLOCKSIZE)
            return header + content + padding

        metadata = (
            "format=KHMPKG-1\n"
            "name=payload-limit-test\n"
            "version=1.0\n"
            "arch=noarch\n"
            "description=Payload limit test\n"
            "depends=\n"
        ).encode()

        manifest = (
            "F\t0644\t" + "0" * 64 + "\tlarge.bin\n"
        ).encode()

        # Build a valid tar prefix manually. The last entry declares
        # an oversized payload, but its actual bytes are never written.
        raw = b"".join([
            member("CONTROL", kind=tarfile.DIRTYPE, mode=0o755),
            member("CONTROL/metadata", metadata),
            member("CONTROL/manifest", manifest),
            member("data", kind=tarfile.DIRTYPE, mode=0o755),
            member(
                "data/large.bin",
                kind=tarfile.REGTYPE,
                mode=0o644,
                declared_size=8 * 1024 * 1024 * 1024 + 1,
            ),
            bytes(2 * tarfile.BLOCKSIZE),
        ])

        oversized.write_bytes(gzip.compress(raw))

        result = run_knpm("verify", oversized)
        self.assert_command_fails(
            result, "oversized uncompressed payload verification"
        )
        self.assertIn("payload exceeds", result.stderr)

        root = self.work / "root"
        root.mkdir()

        result = run_knpm("install", oversized, root=root)
        self.assert_command_fails(
            result, "oversized uncompressed payload installation"
        )
        self.assertIn("payload exceeds", result.stderr)

        self.assertFalse(
            (root / "var/lib/knpm/installed/payload-limit-test").exists()
        )

    def test_14_archive_entry_count_limit_rejected(self):
        oversized = self.work / "too-many-entries.khmpkg"

        def member(name, content=b"", kind=tarfile.REGTYPE, mode=0o644):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.size = len(content)

            header = info.tobuf(format=tarfile.GNU_FORMAT)
            padding = bytes((-len(content)) % tarfile.BLOCKSIZE)
            return header + content + padding

        metadata = (
            "format=KHMPKG-1\n"
            "name=entry-limit-test\n"
            "version=1.0\n"
            "arch=noarch\n"
            "description=Entry count test\n"
            "depends=\n"
        ).encode()

        manifest = b"D\t0755\t-\te000000\n"

        prefix = b"".join([
            member("CONTROL", kind=tarfile.DIRTYPE, mode=0o755),
            member("CONTROL/metadata", metadata),
            member("CONTROL/manifest", manifest),
            member("data", kind=tarfile.DIRTYPE, mode=0o755),
        ])

        # Stream a little more than 250,000 archive headers into gzip.
        # This creates no individual files and keeps memory use bounded.
        with oversized.open("wb") as raw_file:
            with gzip.GzipFile(
                fileobj=raw_file, mode="wb", compresslevel=1
            ) as stream:
                stream.write(prefix)
                batch = bytearray()

                for index in range(250_000):
                    batch.extend(member(
                        f"data/e{index:06d}",
                        kind=tarfile.DIRTYPE,
                        mode=0o755,
                    ))

                    if len(batch) >= 1024 * 1024:
                        stream.write(batch)
                        batch.clear()

                if batch:
                    stream.write(batch)

                stream.write(bytes(2 * tarfile.BLOCKSIZE))

        result = run_knpm("verify", oversized)
        self.assert_command_fails(
            result, "archive entry-count limit verification"
        )
        self.assertIn("archive exceeds the 250000-entry limit", result.stderr)

        root = self.work / "root"
        root.mkdir()

        result = run_knpm("install", oversized, root=root)
        self.assert_command_fails(
            result, "archive entry-count limit installation"
        )
        self.assertIn("archive exceeds the 250000-entry limit", result.stderr)

        self.assertFalse(
            (root / "var/lib/knpm/installed/entry-limit-test").exists()
        )


if __name__ == "__main__":
    if not KNPM.is_file():
        print(f"ERROR: KNPM binary not found: {KNPM}")
    elif not BASH_PACKAGE.is_file():
        print(f"ERROR: test package not found: {BASH_PACKAGE}")
    elif not STAGED_BASH.is_file():
        print(f"ERROR: staged Bash binary not found: {STAGED_BASH}")
    else:
        unittest.main(verbosity=2)
