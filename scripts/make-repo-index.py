#!/usr/bin/env python3
"""Generate a deterministic KHAYMOS-REPO-INDEX-1 file from .khmpkg archives."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

INDEX_MAGIC = "# KHAYMOS-REPO-INDEX-1"
INDEX_FIELDS = (
    "name",
    "version",
    "arch",
    "filename",
    "sha256",
    "depends",
    "description",
)
MAX_METADATA_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~+-]*$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~+-]*\.khmpkg$")


def fail(message: str) -> "NoReturn":
    print(f"make-repo-index: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_member(archive: tarfile.TarFile, member_name: str, size_limit: int) -> bytes:
    matches = [member for member in archive.getmembers() if member.name == member_name]
    if len(matches) != 1:
        raise ValueError(f"archive must contain exactly one {member_name}")

    member = matches[0]
    if not member.isfile() or member.issym() or member.islnk():
        raise ValueError(f"{member_name} must be a regular file")

    if member.size > size_limit:
        raise ValueError(f"{member_name} exceeds the {size_limit}-byte limit")

    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"cannot read {member_name}")

    with stream:
        content = stream.read(size_limit + 1)

    if len(content) > size_limit:
        raise ValueError(f"{member_name} exceeds the {size_limit}-byte limit")

    return content


def parse_package_metadata(raw: bytes, filename: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("CONTROL/metadata is not UTF-8") from exc

    fields: dict[str, str] = {}
    allowed = {"format", "name", "version", "arch", "description", "depends"}

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"malformed metadata line {line_number}")

        key, value = line.split("=", 1)
        if key not in allowed:
            raise ValueError(f"unknown metadata key {key!r}")
        if key in fields:
            raise ValueError(f"duplicate metadata key {key!r}")
        fields[key] = value

    missing = allowed - fields.keys()
    if missing:
        raise ValueError("missing metadata keys: " + ", ".join(sorted(missing)))

    if fields["format"] != "KHMPKG-1":
        raise ValueError("unsupported package format")
    if not NAME_RE.fullmatch(fields["name"]):
        raise ValueError("invalid package name")
    if not VERSION_RE.fullmatch(fields["version"]):
        raise ValueError("invalid package version")
    if fields["arch"] not in {"x86_64", "noarch"}:
        raise ValueError(f"unsupported architecture: {fields['arch']!r}")

    description = fields["description"]
    if any(char in description for char in "\t\r\n"):
        raise ValueError("description contains a tab or line break")

    depends = fields["depends"]
    if depends:
        dependencies = depends.split(",")
        if any(not NAME_RE.fullmatch(dep) for dep in dependencies):
            raise ValueError("invalid dependency list")

    if not FILENAME_RE.fullmatch(filename):
        raise ValueError(f"unsafe package filename: {filename!r}")

    return fields


def package_record(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("package must be a regular, non-symlink file")
    if path.suffix != ".khmpkg":
        raise ValueError("package filename must end in .khmpkg")

    try:
        with tarfile.open(path, mode="r:gz") as archive:
            metadata = read_member(archive, "CONTROL/metadata", MAX_METADATA_BYTES)
            # Require a manifest to exist; KNPM validates its contents on download.
            read_member(archive, "CONTROL/manifest", MAX_MANIFEST_BYTES)
            fields = parse_package_metadata(metadata, path.name)
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"cannot read KHMPKG archive: {exc}") from exc

    return {
        "name": fields["name"],
        "version": fields["version"],
        "arch": fields["arch"],
        "filename": path.name,
        "sha256": digest_file(path),
        "depends": fields["depends"],
        "description": fields["description"],
    }


def collect_records(packages_dir: Path) -> list[dict[str, str]]:
    if packages_dir.is_symlink() or not packages_dir.is_dir():
        raise ValueError(f"packages directory is missing or unsafe: {packages_dir}")

    archives = sorted(packages_dir.glob("*.khmpkg"), key=lambda path: path.name)
    if not archives:
        raise ValueError(f"no .khmpkg files found in {packages_dir}")

    records: list[dict[str, str]] = []
    identities: set[tuple[str, str, str]] = set()

    for archive in archives:
        try:
            record = package_record(archive)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{archive.name}: {exc}") from exc

        identity = (record["name"], record["version"], record["arch"])
        if identity in identities:
            raise ValueError(
                "duplicate package identity: "
                f"{record['name']} {record['version']} ({record['arch']})"
            )
        identities.add(identity)
        records.append(record)

    records.sort(key=lambda record: (
        record["name"], record["arch"], record["version"], record["filename"]
    ))
    return records


def render_index(records: list[dict[str, str]]) -> bytes:
    lines = [INDEX_MAGIC, "\t".join(INDEX_FIELDS)]
    for record in records:
        values = [record[field] for field in INDEX_FIELDS]
        if any(any(char in value for char in "\t\r\n") for value in values):
            raise ValueError(f"index record contains a tab or line break: {record['filename']}")
        lines.append("\t".join(values))
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_index(packages_dir: Path, output: Path) -> int:
    packages_dir = packages_dir.resolve()
    output = output.absolute()

    try:
        output.resolve().relative_to(packages_dir)
    except ValueError:
        pass
    else:
        raise ValueError("index output must be outside the packages directory")

    if output.is_symlink():
        raise ValueError(f"refusing to replace a symlink: {output}")

    records = collect_records(packages_dir)
    data = render_index(records)
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        count = write_index(args.packages_dir, args.output)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"make-repo-index: error: {exc}", file=sys.stderr)
        return 1

    print(f"Repository index: {args.output}")
    print(f"Packages indexed: {count}")
    print(f"Format: {INDEX_MAGIC[2:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
