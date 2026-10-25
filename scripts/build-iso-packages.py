#!/usr/bin/env python3
"""Build the package archives required by the current KhaymOS ISO bootstrap.

This list intentionally records runtime dependencies, not KBuild's build-time
recipe dependencies (for example, the glibc package does not depend on the
temporary cross-bootstrap compiler that built it).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOP = Path(__file__).resolve().parents[1]
DESTDIR = TOP / "destdir"
PACKAGES = TOP / "packages"
BUILDER = TOP / "scripts/khmpkg-build.py"

# name, version, runtime dependencies. Entries must be dependency-first.
PACKAGES_TO_BUILD = [
    ("glibc", "2.44", ""),
    ("linux-firmware-wifi-target", "20261019", ""),
    ("zlib-target", "1.3.2", "glibc"),
    ("gcc-runtime", "16.1.0", "glibc"),
    ("ncurses-target", "6.6", "glibc"),
    ("readline-target", "8.3", "glibc,ncurses-target"),
    ("openssl-target", "3.5.9", "glibc,zlib-target"),
    ("ca-certificates-target", "2026.10.20", ""),
    ("libunistring-target", "1.4.2", "glibc"),
    ("libidn2-target", "2.3.8", "glibc,libunistring-target"),
    ("libpsl-target", "0.23.3", "glibc,libidn2-target,libunistring-target"),
    (
        "curl-target",
        "8.23.0",
        "glibc,openssl-target,zlib-target,ca-certificates-target,libpsl-target",
    ),
    (
        "knpm-target",
        "0.1.0",
        "glibc,gcc-runtime,curl-target,ca-certificates-target",
    ),
    ("bash-target", "5.3", "glibc,readline-target"),
    ("coreutils-target", "9.12", "glibc"),
    ("diffutils-target", "3.12", "glibc"),
    ("findutils-target", "4.10.0", "glibc"),
    ("grep-target", "3.12", "glibc"),
    ("gzip-target", "1.14", "glibc,zlib-target"),
    ("sed-target", "4.10", "glibc"),
    ("tar-target", "1.35", "glibc"),
    ("make-target", "4.4.1", "glibc"),
    ("xz-target", "5.8.4", "glibc"),
    ("expat-target", "2.9.0", "glibc"),
    ("libcap-target", "2.78", "glibc"),
    ("iproute2-target", "7.3.0", "glibc,libcap-target,zlib-target"),
    ("libffi-target", "3.8.0", "glibc"),
    ("pcre2-target", "10.49", "glibc"),
    ("dbus-target", "1.16.2", "glibc,expat-target"),
    ("iwd-target", "3.12", "glibc,dbus-target,openssl-target,readline-target,ca-certificates-target"),
    ("grub-target", "2.14", "glibc"),
    ("kmod-target", "34.2", "glibc,zlib-target,xz-target"),
    ("eudev-target", "3.2.14", "glibc"),
    ("openrc-target", "0.64.1", "glibc,libcap-target"),
    (
        "util-linux-target",
        "2.42.4",
        "glibc,libcap-target,ncurses-target,zlib-target",
    ),
    ("net-tools-target", "2.10", "glibc"),
    ("dhcpcd-target", "10.2.4", "glibc"),
    ("kbd-target", "2.10.0", "glibc,zlib-target,ncurses-target"),
    ("e2fsprogs-target", "1.47.4", "glibc,zlib-target,util-linux-target"),
]


# Rootfs assembly deliberately removes these OpenRC runlevel links so only the
# services selected by KhaymOS are enabled. KNPM's strict "adopt" operation
# verifies every package-manifest path against the assembled rootfs, so the ISO
# bootstrap archive must omit the links that image assembly prunes. Keep service
# scripts themselves in the package: for example, netmount stays installed but
# is not enabled by a runlevel symlink.
PACKAGE_EXCLUDES = {
    "openrc-target": tuple(
        f"etc/runlevels/{level}/{service}"
        for level in ("boot", "default")
        for service in ("hostname", "keymaps", "save-keymaps", "netmount")
    ),
    # Rootfs assembly synthesizes the OpenRC service/config files for dhcpcd.
    # Omit them from package ownership when the upstream staging tree provides them.
    "dhcpcd-target": ("etc/init.d/dhcpcd", "etc/conf.d/dhcpcd"),
}


def fail(message: str) -> "NoReturn":
    print(f"build-iso-packages: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    if not BUILDER.is_file():
        return fail(f"package builder is missing: {BUILDER}")

    PACKAGES.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    for name, version, depends in PACKAGES_TO_BUILD:
        archive_name = f"{name}-{version}.khmpkg"
        stage = DESTDIR / f"{name}-{version}"
        output = PACKAGES / archive_name

        if not stage.is_dir() or stage.is_symlink():
            return fail(f"staging directory is missing or unsafe: {stage}")

        missing = [dependency for dependency in depends.split(",") if dependency and dependency not in seen]
        if missing:
            return fail(
                f"{name}: runtime dependency/dependencies must be listed earlier: "
                + ", ".join(missing)
            )

        excluded_paths = PACKAGE_EXCLUDES.get(name, ())
        with tempfile.TemporaryDirectory(prefix="khaymos-iso-package-") as temp_dir:
            package_stage = stage
            if excluded_paths:
                package_stage = Path(temp_dir) / "stage"
                shutil.copytree(stage, package_stage, symlinks=True)

                for relative in excluded_paths:
                    candidate = package_stage / relative
                    if candidate.is_symlink() or candidate.is_file():
                        candidate.unlink()
                    elif candidate.exists():
                        return fail(
                            f"{name}: expected an excluded file or symlink, got: {relative}"
                        )

            command = [
                sys.executable,
                str(BUILDER),
                "--name",
                name,
                "--version",
                version,
                "--stage",
                str(package_stage),
                "--output",
                str(output),
                "--arch",
                "x86_64",
                "--description",
                f"KhaymOS system package {name}",
            ]
            if depends:
                command.extend(["--depends", depends])

            print(f"=== Packaging {name} {version} ===", flush=True)
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                return fail(f"failed to package {name}; see the error above")

        seen.add(name)

    print(f"\nBuilt {len(PACKAGES_TO_BUILD)} ISO bootstrap package archives.")
    print(f"Directory: {PACKAGES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
