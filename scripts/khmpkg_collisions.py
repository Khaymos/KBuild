#!/usr/bin/env python3
"""Detect conflicting file ownership across KHMPKG archives."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys
import tarfile


Entry = tuple[str, str, str]


@dataclass(frozen=True)
class PackageArchive:
    name: str
    version: str
    path: Path
    entries: dict[str, Entry]


@dataclass(frozen=True)
class Collision:
    path: str
    left: PackageArchive
    left_entry: Entry
    right: PackageArchive
    right_entry: Entry


def read_member(archive: tarfile.TarFile, name: str) -> bytes:
    try:
        member = archive.extractfile(name)
    except KeyError as exc:
        raise ValueError(f"missing {name}") from exc

    if member is None:
        raise ValueError(f"cannot read {name}")

    with member:
        return member.read()


def read_package_archive(path: Path) -> PackageArchive:
    """Read package identity and manifest without extracting its payload."""
    with tarfile.open(path, "r:*") as archive:
        metadata_text = read_member(
            archive, "CONTROL/metadata"
        ).decode("utf-8")
        manifest_text = read_member(
            archive, "CONTROL/manifest"
        ).decode("utf-8")

    metadata = {}
    for line in metadata_text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            metadata[key] = value

    if metadata.get("format") != "KHMPKG-1":
        raise ValueError("unsupported or missing package format")

    name = metadata.get("name", "")
    version = metadata.get("version", "")

    if not name or not version:
        raise ValueError("package metadata is missing name or version")

    entries: dict[str, Entry] = {}

    for line_number, line in enumerate(manifest_text.splitlines(), start=1):
        if not line:
            continue

        fields = line.split("\t", 3)
        if len(fields) != 4:
            raise ValueError(
                f"malformed manifest line {line_number}"
            )

        kind, mode, digest, relative_path = fields

        if kind not in ("F", "D", "L"):
            raise ValueError(
                f"invalid entry type {kind!r} on manifest line {line_number}"
            )

        if not relative_path:
            raise ValueError(
                f"empty path on manifest line {line_number}"
            )

        if relative_path in entries:
            raise ValueError(
                f"duplicate path in manifest: {relative_path}"
            )

        entries[relative_path] = (kind, mode, digest)

    return PackageArchive(
        name=name,
        version=version,
        path=path.resolve(),
        entries=entries,
    )


def load_archives(
    directory: Path,
    exclude_path: Path | None = None,
) -> list[PackageArchive]:
    """Load all sibling packages; an unreadable archive is a hard error."""
    directory = directory.resolve()

    if not directory.is_dir():
        raise ValueError(f"package directory does not exist: {directory}")

    excluded = exclude_path.resolve() if exclude_path is not None else None
    packages = []

    for path in sorted(directory.glob("*.khmpkg")):
        if excluded is not None and path.resolve() == excluded:
            continue

        try:
            packages.append(read_package_archive(path))
        except Exception as exc:
            raise ValueError(
                f"cannot inspect package archive {path}: {exc}"
            ) from exc

    return packages


def find_collisions(packages: list[PackageArchive]) -> list[Collision]:
    """Find shared non-directory paths claimed by different package names."""
    claims: dict[str, list[tuple[PackageArchive, Entry]]] = defaultdict(list)

    for package in packages:
        for relative_path, entry in package.entries.items():
            claims[relative_path].append((package, entry))

    collisions = []

    for relative_path, path_claims in sorted(claims.items()):
        for index, (left, left_entry) in enumerate(path_claims):
            for right, right_entry in path_claims[index + 1:]:
                # Different versions of one package are alternatives.
                if left.name == right.name:
                    continue

                # Multiple packages may create or use the same directory.
                if left_entry[0] == "D" and right_entry[0] == "D":
                    continue

                collisions.append(
                    Collision(
                        path=relative_path,
                        left=left,
                        left_entry=left_entry,
                        right=right,
                        right_entry=right_entry,
                    )
                )

    return collisions


def format_collision(collision: Collision) -> str:
    if collision.left_entry == collision.right_entry:
        description = "duplicate ownership of an identical entry"
    else:
        description = "conflicting path type, mode, or contents"

    def describe(package: PackageArchive, entry: Entry) -> str:
        kind, mode, digest = entry
        return (
            f"{package.name} {package.version} "
            f"[{package.path.name}]: "
            f"type={kind}, mode={mode}, sha256={digest}"
        )

    return (
        f"{description}: {collision.path}\n"
        f"  {describe(collision.left, collision.left_entry)}\n"
        f"  {describe(collision.right, collision.right_entry)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
    )
    parser.add_argument(
        "--packages-dir",
        type=Path,
        default=Path("packages"),
        help="directory containing .khmpkg archives (default: packages)",
    )
    args = parser.parse_args()

    try:
        packages = load_archives(args.packages_dir)
    except Exception as exc:
        print(f"khmpkg-collisions: error: {exc}", file=sys.stderr)
        return 2

    print(
        f"Scanned {len(packages)} package archives in "
        f"{args.packages_dir.resolve()}"
    )

    collisions = find_collisions(packages)

    if collisions:
        print("\n=== Package path collisions ===", file=sys.stderr)
        for collision in collisions:
            print(format_collision(collision), file=sys.stderr)
            print(file=sys.stderr)

        print(
            f"FAILED: {len(collisions)} conflicting path claim(s) found.",
            file=sys.stderr,
        )
        return 1

    print("PASS: no shared non-directory paths between different package names.")
    print("Shared directories and alternate versions of the same package are allowed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
