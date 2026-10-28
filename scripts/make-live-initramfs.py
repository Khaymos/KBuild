#!/usr/bin/env python3
"""Create a small initramfs for the SquashFS-based KhaymOS live ISO."""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TOOLS = (
    ("usr/bin/bash", "usr/bin/bash"),
    ("usr/bin/mount", "usr/bin/mount"),
    ("usr/bin/umount", "usr/bin/umount"),
    ("usr/bin/mkdir", "usr/bin/mkdir"),
    ("usr/bin/sleep", "usr/bin/sleep"),
    ("usr/sbin/switch_root", "usr/sbin/switch_root"),
)
NEEDED_RE = re.compile(r"\(NEEDED\).*Shared library: \[(.+?)\]")
INTERP_RE = re.compile(r"Requesting program interpreter: (.+?)\]")


def readelf(flag: str, path: Path) -> str:
    result = subprocess.run(
        ["readelf", flag, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def find_library(rootfs: Path, name: str) -> Path:
    for directory in ("usr/lib", "lib", "lib64", "usr/lib64"):
        candidate = rootfs / directory / name
        if candidate.exists():
            return candidate.resolve(strict=True)
    raise RuntimeError(f"cannot find target runtime library {name!r} in {rootfs}")


def copy_elf(source: Path, destination: Path, rootfs: Path, tree: Path, copied: set[str]) -> None:
    source = source.resolve(strict=True)
    key = str(source)
    if key in copied:
        return
    copied.add(key)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(source.stat().st_mode & 0o777)

    interpreter_match = INTERP_RE.search(readelf("-l", source))
    if interpreter_match:
        interpreter = interpreter_match.group(1).lstrip("/")
        interpreter_source = (rootfs / interpreter).resolve(strict=True)
        interpreter_destination = tree / interpreter
        interpreter_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(interpreter_source, interpreter_destination)
        interpreter_destination.chmod(interpreter_source.stat().st_mode & 0o777)

    for library in NEEDED_RE.findall(readelf("-d", source)):
        library_source = find_library(rootfs, library)
        library_destination = tree / "usr/lib" / library
        if not library_destination.exists():
            copy_elf(library_source, library_destination, rootfs, tree, copied)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rootfs = args.rootfs.resolve(strict=True)
    output = args.output.resolve()

    for tool in ("readelf", "cpio", "gzip", "bash"):
        if shutil.which(tool) is None:
            parser.error(f"required host tool missing: {tool}")

    with tempfile.TemporaryDirectory(prefix="khaymos-initramfs-") as temp:
        tree = Path(temp) / "tree"
        tree.mkdir()
        copied: set[str] = set()

        for source_rel, target_rel in TOOLS:
            source = rootfs / source_rel
            if not source.is_file():
                raise RuntimeError(f"required initramfs executable missing: {source}")
            copy_elf(source, tree / target_rel, rootfs, tree, copied)

        (tree / "bin").mkdir(exist_ok=True)
        (tree / "bin/bash").symlink_to("../usr/bin/bash")
        for directory in ("dev", "proc", "sys", "run", "newroot", "lower", "iso"):
            (tree / directory).mkdir(parents=True, exist_ok=True)
        for name, mode, device in (
            ("console", 0o600, (5, 1)),
            ("null", 0o666, (1, 3)),
            ("tty", 0o666, (5, 0)),
        ):
            node = tree / "dev" / name
            os.mknod(node, mode | 0o20000, os.makedev(*device))

        init = tree / "init"
        init.write_text("""#!/bin/bash
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
export TERM=linux
mount -t devtmpfs devtmpfs /dev || echo '[WARN] devtmpfs mount failed'
mkdir -p /dev/pts /dev/shm /proc /sys /run /run/iso /run/lower /run/overlay /newroot
mount -t devpts devpts /dev/pts || echo '[WARN] devpts mount failed'
mount -t proc proc /proc || echo '[WARN] proc mount failed'
mount -t sysfs sysfs /sys || echo '[WARN] sysfs mount failed'
echo 'KhaymOS live: searching for installation media...'
media=''
for attempt in {1..30}; do
    for device in /dev/sr* /dev/sd* /dev/nvme*n* /dev/mmcblk*; do
        [[ -b "$device" ]] || continue
        if mount -t iso9660 -o ro "$device" /run/iso 2>/dev/null; then
            if [[ -s /run/iso/live/rootfs.squashfs ]]; then
                media="$device"
                break 2
            fi
            umount /run/iso 2>/dev/null || true
        fi
    done
    sleep 1
done
if [[ -z "$media" ]]; then
    echo 'KhaymOS live: ERROR: could not find ISO media containing /live/rootfs.squashfs.'
    echo 'Available block devices:'
    for device in /dev/sr* /dev/sd* /dev/nvme*n* /dev/mmcblk*; do
        [[ -b "$device" ]] && echo "  $device"
    done
    exec /usr/bin/bash
fi
echo "KhaymOS live: found media at $media; mounting SquashFS..."
if ! mount -t squashfs -o loop,ro /run/iso/live/rootfs.squashfs /run/lower; then
    echo 'KhaymOS live: ERROR: could not mount the SquashFS image.'
    exec /usr/bin/bash
fi
mount -t tmpfs -o mode=0755 tmpfs /run/overlay || {
    echo 'KhaymOS live: ERROR: could not create writable overlay.'
    exec /usr/bin/bash
}
mkdir -p /run/overlay/upper /run/overlay/work
if ! mount -t overlay overlay -o lowerdir=/run/lower,upperdir=/run/overlay/upper,workdir=/run/overlay/work /newroot; then
    echo 'KhaymOS live: ERROR: could not mount the writable root overlay.'
    exec /usr/bin/bash
fi
echo 'KhaymOS live: handing over to OpenRC...'
exec switch_root /newroot /sbin/init
""", encoding="utf-8")
        init.chmod(0o755)

        output.parent.mkdir(parents=True, exist_ok=True)
        command = "find . -print0 | cpio --null -o --format=newc | gzip -9 > "$1""
        subprocess.run(["bash", "-o", "pipefail", "-c", command, "_", str(output)], cwd=tree, check=True)

    print(f"Created minimal live initramfs: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
