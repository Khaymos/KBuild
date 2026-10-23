#!/usr/bin/env python3
"""Generate a Meson cross file for the active KBuild target toolchain."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path


def meson_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def meson_array(values: list[str]) -> str:
    return "[" + ", ".join(meson_string(value) for value in values) + "]"


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} OUTPUT.ini", file=sys.stderr)
        return 2

    target = os.environ.get("KBUILD_TARGET", "")
    toolchain_value = os.environ.get("KBUILD_TOOLCHAIN")
    sysroot_value = os.environ.get("SYSROOT")
    if not target or not toolchain_value or not sysroot_value:
        print("KBuild target, toolchain, and sysroot environment variables are required", file=sys.stderr)
        return 2

    cpu = target.split("-", 1)[0]
    machine_details = {
        "x86_64": ("x86_64", "little"),
        "aarch64": ("aarch64", "little"),
        "i686": ("x86", "little"),
        "i386": ("x86", "little"),
        "arm": ("arm", "little"),
        "armv7": ("arm", "little"),
        "riscv64": ("riscv64", "little"),
    }
    if cpu not in machine_details:
        print(f"unsupported Meson cross-file CPU architecture: {cpu}", file=sys.stderr)
        return 2

    cpu_family, endian = machine_details[cpu]
    toolchain = Path(toolchain_value).resolve()
    sysroot = Path(sysroot_value).resolve()
    bindir = toolchain / "bin"
    compiler = bindir / f"{target}-gcc"
    archiver = bindir / f"{target}-ar"
    stripper = bindir / f"{target}-strip"
    pkg_config = shutil.which("pkg-config", path=os.environ.get("PATH"))

    for executable in (compiler, archiver, stripper):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            print(f"required target tool is missing or not executable: {executable}", file=sys.stderr)
            return 2
    if pkg_config is None:
        print("host pkg-config is required for this Meson cross build", file=sys.stderr)
        return 2

    pkgconfig_dirs = [
        str(sysroot / "usr/lib/pkgconfig"),
        str(sysroot / "usr/share/pkgconfig"),
        str(sysroot / "usr/lib64/pkgconfig"),
    ]
    c_args = shlex.split(os.environ.get("CFLAGS", ""))
    c_link_args = shlex.split(os.environ.get("LDFLAGS", ""))

    contents = "\n".join([
        "[binaries]",
        f"c = {meson_string(str(compiler))}",
        f"ar = {meson_string(str(archiver))}",
        f"strip = {meson_string(str(stripper))}",
        f"pkg-config = {meson_string(pkg_config)}",
        "",
        "[properties]",
        f"sys_root = {meson_string(str(sysroot))}",
        f"pkg_config_libdir = {meson_array(pkgconfig_dirs)}",
        "needs_exe_wrapper = true",
        "",
        "[host_machine]",
        "system = 'linux'",
        f"cpu_family = {meson_string(cpu_family)}",
        f"cpu = {meson_string(cpu)}",
        f"endian = {meson_string(endian)}",
        "",
        "[built-in options]",
        f"c_args = {meson_array(c_args)}",
        f"c_link_args = {meson_array(c_link_args)}",
        "",
    ])

    output = Path(sys.argv[1])
    output.write_text(contents, encoding="utf-8")
    print(f"Wrote Meson cross file for {target}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
