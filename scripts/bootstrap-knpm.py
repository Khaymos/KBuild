#!/usr/bin/env python3
"""Bootstrap the KNPM database for the KhaymOS rootfs before ISO creation."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

TOP = Path(__file__).resolve().parents[1]
ROOTFS = Path(sys.argv[1]) if len(sys.argv) > 1 else TOP / "rootfs"

ROOTFS = ROOTFS.absolute()
PROJECT_ROOT = TOP.parent.parent.parent
KNPM_CANDIDATES = (
    PROJECT_ROOT / "knpm/gh/knpm/target/release/knpm",
    PROJECT_ROOT / "knpm/target/release/knpm",
)
KNPM_HOST = next((candidate for candidate in KNPM_CANDIDATES if candidate.is_file()), KNPM_CANDIDATES[0])
KNPM_STAGE = TOP / "destdir/knpm-target-0.1.0/usr/bin/knpm"
PACKAGES = TOP / "packages"

BOOTSTRAP_PACKAGES = [
    ("glibc", "2.44", "glibc-2.44.khmpkg"),
    ("linux-firmware-wifi-target", "20261019", "linux-firmware-wifi-target-20261019.khmpkg"),
    ("zlib-target", "1.3.2", "zlib-target-1.3.2.khmpkg"),
    ("gcc-runtime", "16.1.0", "gcc-runtime-16.1.0.khmpkg"),
    ("ncurses-target", "6.6", "ncurses-target-6.6.khmpkg"),
    ("readline-target", "8.3", "readline-target-8.3.khmpkg"),
    ("openssl-target", "3.5.9", "openssl-target-3.5.9.khmpkg"),
    ("ca-certificates-target", "2026.10.20", "ca-certificates-target-2026.10.20.khmpkg"),
    ("libunistring-target", "1.4.2", "libunistring-target-1.4.2.khmpkg"),
    ("libidn2-target", "2.3.8", "libidn2-target-2.3.8.khmpkg"),
    ("libpsl-target", "0.23.3", "libpsl-target-0.23.3.khmpkg"),
    ("curl-target", "8.23.0", "curl-target-8.23.0.khmpkg"),
    ("knpm-target", "0.1.0", "knpm-target-0.1.0.khmpkg"),
    ("bash-target", "5.3", "bash-target-5.3.khmpkg"),
    ("coreutils-target", "9.12", "coreutils-target-9.12.khmpkg"),
    ("diffutils-target", "3.12", "diffutils-target-3.12.khmpkg"),
    ("findutils-target", "4.10.0", "findutils-target-4.10.0.khmpkg"),
    ("grep-target", "3.12", "grep-target-3.12.khmpkg"),
    ("gzip-target", "1.14", "gzip-target-1.14.khmpkg"),
    ("sed-target", "4.10", "sed-target-4.10.khmpkg"),
    ("tar-target", "1.35", "tar-target-1.35.khmpkg"),
    ("make-target", "4.4.1", "make-target-4.4.1.khmpkg"),
    ("xz-target", "5.8.4", "xz-target-5.8.4.khmpkg"),
    ("expat-target", "2.9.0", "expat-target-2.9.0.khmpkg"),
    ("libcap-target", "2.78", "libcap-target-2.78.khmpkg"),
    ("iproute2-target", "7.3.0", "iproute2-target-7.3.0.khmpkg"),
    ("libffi-target", "3.8.0", "libffi-target-3.8.0.khmpkg"),
    ("pcre2-target", "10.49", "pcre2-target-10.49.khmpkg"),
    ("dbus-target", "1.16.2", "dbus-target-1.16.2.khmpkg"),
    ("iwd-target", "3.12", "iwd-target-3.12.khmpkg"),
    ("grub-target", "2.14", "grub-target-2.14.khmpkg"),
    ("kmod-target", "34.2", "kmod-target-34.2.khmpkg"),
    ("eudev-target", "3.2.14", "eudev-target-3.2.14.khmpkg"),
    ("openrc-target", "0.64.1", "openrc-target-0.64.1.khmpkg"),
    ("util-linux-target", "2.42.4", "util-linux-target-2.42.4.khmpkg"),
    ("net-tools-target", "2.10", "net-tools-target-2.10.khmpkg"),
    ("dhcpcd-target", "10.2.4", "dhcpcd-target-10.2.4.khmpkg"),
    ("kbd-target", "2.10.0", "kbd-target-2.10.0.khmpkg"),
    ("e2fsprogs-target", "1.47.4", "e2fsprogs-target-1.47.4.khmpkg"),
]


def fail(message):
    print(f"bootstrap-knpm: error: {message}", file=sys.stderr)
    return 1


def record_version(record):
    metadata = record / "metadata"
    if record.is_symlink() or not record.is_dir():
        raise RuntimeError(f"invalid package record: {record}")

    if metadata.is_symlink() or not metadata.is_file():
        raise RuntimeError(f"invalid package metadata: {metadata}")

    fields = {}
    for line in metadata.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value

    return fields.get("name"), fields.get("version")


def run_knpm(*args, root=None):
    env = os.environ.copy()
    if root is not None:
        env["KNPM_ROOT"] = str(root)

    return subprocess.run(
        [str(KNPM_HOST), *(str(arg) for arg in args)],
        env=env,
        check=False,
    )


def main():
    if os.geteuid() != 0:
        return fail("run this bootstrap script as root, usually through build-iso.sh")

    if ROOTFS.is_symlink() or not ROOTFS.is_dir():
        return fail(f"rootfs is missing or is not a real directory: {ROOTFS}")

    if not KNPM_HOST.is_file() or not os.access(KNPM_HOST, os.X_OK):
        return fail(f"host KNPM executable is missing: {KNPM_HOST}")

    for name, version, filename in BOOTSTRAP_PACKAGES:
        archive = PACKAGES / filename
        if not archive.is_file() or archive.is_symlink():
            return fail(f"required package archive is missing: {archive}")

        print(f"=== Verify {name} {version} ===", flush=True)
        result = run_knpm("verify", archive)
        if result.returncode != 0:
            return fail(f"package verification failed: {filename}")

    installed_dir = ROOTFS / "var/lib/knpm/installed"
    knpm_record = installed_dir / "knpm-target"

    # On first bootstrap, make the rootfs binary match the staged payload
    # that was used to create knpm-target-0.1.0.khmpkg.
    # Never overwrite an already-registered KNPM package.
    if not knpm_record.exists() and not knpm_record.is_symlink():
        if not KNPM_STAGE.is_file() or KNPM_STAGE.is_symlink():
            return fail(f"staged KNPM executable is missing: {KNPM_STAGE}")

        destination = ROOTFS / "usr/bin/knpm"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(KNPM_STAGE, destination)
        destination.chmod(KNPM_STAGE.stat().st_mode & 0o7777)
        print("Prepared /usr/bin/knpm for initial adoption.", flush=True)

    if not (ROOTFS / "usr/bin/knpm").is_file():
        return fail("rootfs/usr/bin/knpm is missing")

    for name, version, filename in BOOTSTRAP_PACKAGES:
        record = installed_dir / name
        archive = PACKAGES / filename

        if record.exists() or record.is_symlink():
            try:
                existing_name, existing_version = record_version(record)
            except (OSError, RuntimeError) as error:
                return fail(str(error))

            if existing_name != name or existing_version != version:
                return fail(
                    f"{name} has an existing record with unexpected identity/version: "
                    f"{existing_name} {existing_version}; refusing to overwrite it"
                )

            print(f"Already registered: {name} {version}", flush=True)
            continue

        action = "adopt"
        print(f"=== {action.capitalize()} {name} {version} ===", flush=True)
        result = run_knpm(action, archive, root=ROOTFS)
        if result.returncode != 0:
            return fail(f"{action} failed for {name}; ISO build must stop")

    print("\n=== Final bootstrap package inventory ===", flush=True)
    result = run_knpm("list", root=ROOTFS)
    if result.returncode != 0:
        return fail("could not read the KNPM package database")

    print("\nKNPM bootstrap complete.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
