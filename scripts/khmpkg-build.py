#!/usr/bin/env python3
"""Build a KHMPKG v1 archive from a KBuild staging directory."""

import argparse
import gzip
import hashlib
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from khmpkg_collisions import (
    PackageArchive,
    find_collisions,
    format_collision,
    load_archives,
)


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~+-]*$")
ARCH_RE = re.compile(r"^(x86_64|noarch)$")


def fail(message: str) -> "NoReturn":
    print(f"khmpkg-build: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_relpath(relative: str) -> None:
    if not relative or relative.startswith("/"):
        fail(f"unsafe package path: {relative!r}")

    if any(ch in relative for ch in "\t\r\n"):
        fail(f"package paths cannot contain tabs or line breaks: {relative!r}")

    path = PurePosixPath(relative)
    if any(part in ("", ".", "..") for part in path.parts):
        fail(f"unsafe package path: {relative!r}")


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_link(target: str) -> str:
    if any(ch in target for ch in "\t\r\n"):
        fail(f"symbolic-link targets cannot contain tabs or line breaks: {target!r}")
    return hashlib.sha256(os.fsencode(target)).hexdigest()


def walk_stage(stage: Path):
    """Yield sorted (relative_path, type, mode, hash) records."""
    entries = []

    for current, dirnames, filenames in os.walk(
        stage, topdown=True, followlinks=False
    ):
        dirnames.sort()
        filenames.sort()

        # os.walk lists symlinked directories in dirnames but does not follow
        # them with followlinks=False; lstat lets us record them as links.
        for name in dirnames + filenames:
            source = Path(current) / name
            relative = source.relative_to(stage).as_posix()
            validate_relpath(relative)

            info = source.lstat()
            mode = f"{stat.S_IMODE(info.st_mode):04o}"

            if stat.S_ISDIR(info.st_mode):
                kind, digest = "D", "-"
            elif stat.S_ISREG(info.st_mode):
                kind, digest = "F", digest_file(source)
            elif stat.S_ISLNK(info.st_mode):
                kind = "L"
                digest = digest_link(os.readlink(source))
            else:
                fail(f"unsupported special file: {relative}")

            entries.append((relative, kind, mode, digest))

    yield from sorted(entries, key=lambda entry: os.fsencode(entry[0]))


def normalize_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--description", default="KhaymOS package")
    parser.add_argument("--depends", default="")
    args = parser.parse_args()

    if not NAME_RE.fullmatch(args.name):
        fail(f"invalid package name: {args.name!r}")
    if not VERSION_RE.fullmatch(args.version):
        fail(f"invalid package version: {args.version!r}")
    if not ARCH_RE.fullmatch(args.arch):
        fail(f"unsupported architecture: {args.arch!r}")
    if any(ch in args.description for ch in "\r\n"):
        fail("description cannot contain line breaks")

    dependencies = [item for item in args.depends.split(",") if item]
    for dependency in dependencies:
        if not NAME_RE.fullmatch(dependency):
            fail(f"invalid dependency name: {dependency!r}")

    stage = args.stage.resolve()
    if not stage.is_dir():
        fail(f"staging directory does not exist: {stage}")
    if stage.is_symlink():
        fail("staging root must not be a symbolic link")

    output = args.output.resolve()
    if output.is_relative_to(stage):
        fail("output archive must not be inside the staging directory")

    entries = list(walk_stage(stage))
    if not entries:
        fail("refusing to create an empty package")

    # Validate the candidate against the existing package collection before
    # opening the output archive, so collisions cannot overwrite an archive.
    if output.parent.is_dir():
        try:
            existing_packages = load_archives(
                output.parent,
                exclude_path=output,
            )
        except Exception as exc:
            fail(f"cannot validate existing package archives: {exc}")

        candidate_entries = {
            relative: (kind, mode, digest)
            for relative, kind, mode, digest in entries
        }
        candidate = PackageArchive(
            name=args.name,
            version=args.version,
            path=output,
            entries=candidate_entries,
        )

        collisions = find_collisions(existing_packages + [candidate])
        if collisions:
            for collision in collisions:
                print(
                    f"khmpkg-build: collision:\n"
                    f"{format_collision(collision)}",
                    file=sys.stderr,
                )

            fail(
                f"refusing to create {output.name}: "
                f"{len(collisions)} conflicting path claim(s) found "
                f"in {output.parent}"
            )

    output.parent.mkdir(parents=True, exist_ok=True)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="khmpkg-build-") as temp:
        control = Path(temp) / "CONTROL"
        control.mkdir()

        metadata = [
            "format=KHMPKG-1",
            f"name={args.name}",
            f"version={args.version}",
            f"arch={args.arch}",
            f"description={args.description}",
            f"depends={','.join(dependencies)}",
        ]
        (control / "metadata").write_text(
            "\n".join(metadata) + "\n", encoding="utf-8"
        )

        with (control / "manifest").open("w", encoding="utf-8", newline="\n") as stream:
            for relative, kind, mode, digest in entries:
                stream.write(f"{kind}\t{mode}\t{digest}\t{relative}\n")

        with output.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    archive.add(
                        control, arcname="CONTROL", recursive=True,
                        filter=normalize_tarinfo
                    )
                    # Add the payload root without recursing.
                    archive.add(
                        stage, arcname="data", recursive=False,
                        filter=normalize_tarinfo
                    )

                    # Add manifest entries individually. Clear tarfile's
                    # inode cache before each entry so filesystem hard links
                    # become separate regular files in the archive. Keep
                    # dereference disabled so symbolic links stay symbolic.
                    for relative, kind, mode, digest in entries:
                        archive.inodes.clear()
                        archive.add(
                            stage / relative,
                            arcname=f"data/{relative}",
                            recursive=False,
                            filter=normalize_tarinfo,
                        )

    print(f"Package:  {output}")
    print(f"Name:     {args.name}")
    print(f"Version:  {args.version}")
    print(f"Arch:     {args.arch}")
    print(f"Entries:  {len(entries)}")
    print(f"Size:     {output.stat().st_size} bytes")
    print(f"SHA-256:  {digest_file(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
