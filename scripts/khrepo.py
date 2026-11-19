#!/usr/bin/env python3
"""KhaymOS repository manager: SSH client and privileged repository server.

The same file runs on the developer workstation and on the repository host.
No third-party Python modules are required.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator


INDEX_MAGIC = "# KHAYMOS-REPO-INDEX-1"
INDEX_FIELDS = (
    "name", "version", "arch", "filename", "sha256", "depends", "description"
)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~+-]*$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~+-]*\.khmpkg$")
TRASH_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}--[A-Za-z0-9][A-Za-z0-9+_.:~+-]*\.khmpkg$"
)
MAX_METADATA_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = int(os.environ.get("KHREPO_MAX_PACKAGE_BYTES", str(8 * 1024**3)))
MAX_BATCH_BYTES = int(os.environ.get("KHREPO_MAX_BATCH_BYTES", str(24 * 1024**3)))
DEFAULT_HOST = os.environ.get("KHAYMOS_REPO_HOST", "khaymos-repo")
DEFAULT_SERVER_COMMAND = os.environ.get(
    "KHAYMOS_REPO_SERVER_COMMAND", "/usr/local/sbin/khrepo"
)
DEFAULT_REPO_ROOT = Path(
    os.environ.get("KHREPO_REPO_ROOT", "/srv/khaymos/repo")
)


class RepoError(Exception):
    """An expected repository-manager failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_package_filename(filename: str) -> bool:
    return bool(FILENAME_RE.fullmatch(filename)) and Path(filename).name == filename


def read_tar_member(archive: tarfile.TarFile, member_name: str, size_limit: int) -> bytes:
    matches = [member for member in archive.getmembers() if member.name == member_name]
    if len(matches) != 1:
        raise RepoError(f"archive must contain exactly one {member_name}")

    member = matches[0]
    if not member.isfile() or member.issym() or member.islnk():
        raise RepoError(f"{member_name} must be a regular file")
    if member.size > size_limit:
        raise RepoError(f"{member_name} exceeds the {size_limit}-byte limit")

    stream = archive.extractfile(member)
    if stream is None:
        raise RepoError(f"cannot read {member_name}")
    with stream:
        raw = stream.read(size_limit + 1)
    if len(raw) > size_limit:
        raise RepoError(f"{member_name} exceeds the {size_limit}-byte limit")
    return raw


def parse_metadata(raw: bytes, filename: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepoError("CONTROL/metadata is not UTF-8") from exc

    fields: dict[str, str] = {}
    allowed = {"format", "name", "version", "arch", "description", "depends"}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        if "=" not in line:
            raise RepoError(f"malformed metadata line {line_number}")
        key, value = line.split("=", 1)
        if key not in allowed:
            raise RepoError(f"unknown metadata key {key!r}")
        if key in fields:
            raise RepoError(f"duplicate metadata key {key!r}")
        fields[key] = value

    missing = allowed - fields.keys()
    if missing:
        raise RepoError("missing metadata keys: " + ", ".join(sorted(missing)))
    if fields["format"] != "KHMPKG-1":
        raise RepoError("unsupported package format")
    if not NAME_RE.fullmatch(fields["name"]):
        raise RepoError("invalid package name")
    if not VERSION_RE.fullmatch(fields["version"]):
        raise RepoError("invalid package version")
    if fields["arch"] not in {"x86_64", "noarch"}:
        raise RepoError(f"unsupported architecture: {fields['arch']!r}")
    if any(char in fields["description"] for char in "\t\r\n"):
        raise RepoError("description contains a tab or line break")
    if fields["depends"]:
        dependencies = fields["depends"].split(",")
        if any(not NAME_RE.fullmatch(dep) for dep in dependencies):
            raise RepoError("invalid dependency list")
    if not safe_package_filename(filename):
        raise RepoError(f"unsafe package filename: {filename!r}")
    return fields


def package_record(path: Path, filename: str | None = None) -> dict[str, str]:
    display_name = filename or path.name
    if path.is_symlink() or not path.is_file():
        raise RepoError("package must be a regular, non-symlink file")
    if not safe_package_filename(display_name):
        raise RepoError(f"unsafe package filename: {display_name!r}")
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise RepoError(f"package exceeds the {MAX_PACKAGE_BYTES}-byte limit")

    try:
        with tarfile.open(path, mode="r:gz") as archive:
            metadata = read_tar_member(archive, "CONTROL/metadata", MAX_METADATA_BYTES)
            read_tar_member(archive, "CONTROL/manifest", MAX_MANIFEST_BYTES)
            fields = parse_metadata(metadata, display_name)
    except (OSError, tarfile.TarError) as exc:
        raise RepoError(f"cannot read KHMPKG archive: {exc}") from exc

    return {
        "name": fields["name"],
        "version": fields["version"],
        "arch": fields["arch"],
        "filename": display_name,
        "sha256": sha256_file(path),
        "depends": fields["depends"],
        "description": fields["description"],
    }


def validate_unique(records: list[dict[str, str]]) -> None:
    identities: dict[tuple[str, str, str], str] = {}
    filenames: set[str] = set()
    for record in records:
        filename = record["filename"]
        if filename in filenames:
            raise RepoError(f"duplicate package filename: {filename}")
        filenames.add(filename)
        identity = (record["name"], record["version"], record["arch"])
        previous = identities.get(identity)
        if previous is not None:
            raise RepoError(
                "duplicate package identity: "
                f"{record['name']} {record['version']} ({record['arch']}) "
                f"in {previous} and {filename}"
            )
        identities[identity] = filename


def collect_records(packages_dir: Path) -> list[dict[str, str]]:
    if packages_dir.is_symlink() or not packages_dir.is_dir():
        raise RepoError(f"packages directory is missing or unsafe: {packages_dir}")

    records: list[dict[str, str]] = []
    for path in sorted(packages_dir.glob("*.khmpkg"), key=lambda item: item.name):
        try:
            records.append(package_record(path))
        except (OSError, RepoError) as exc:
            raise RepoError(f"{path.name}: {exc}") from exc

    validate_unique(records)
    records.sort(key=lambda item: (
        item["name"], item["arch"], item["version"], item["filename"]
    ))
    return records


def render_index(records: list[dict[str, str]]) -> bytes:
    validate_unique(records)
    lines = [INDEX_MAGIC, "\t".join(INDEX_FIELDS)]
    for record in sorted(records, key=lambda item: (
        item["name"], item["arch"], item["version"], item["filename"]
    )):
        values = [record[field] for field in INDEX_FIELDS]
        if any(any(char in value for char in "\t\r\n") for value in values):
            raise RepoError(f"index record contains a tab or line break: {record['filename']}")
        lines.append("\t".join(values))
    return ("\n".join(lines) + "\n").encode("utf-8")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_staged_index(index_path: Path, records: list[dict[str, str]]) -> Path:
    data = render_index(records)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.", suffix=".tmp", dir=index_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        return temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def publish_staged_index(temporary_path: Path, index_path: Path) -> None:
    os.replace(temporary_path, index_path)
    fsync_directory(index_path.parent)


def timestamp_id(filename: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}--{filename}"


class Repository:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self.arch_root = self.root / "x86_64" / "khaymos"
        self.packages = self.arch_root / "packages"
        self.index = self.arch_root / "index.tsv"
        self.mirrorlist = self.root / "mirrorlists" / "official.list"
        self.state = self.root / ".khrepo"
        self.incoming = self.state / "incoming"
        self.trash = self.state / "trash"
        self.backups = self.state / "backups"
        self.lock_path = self.state / "lock"

    def prepare(self) -> None:
        for directory in (
            self.arch_root, self.packages, self.root / "mirrorlists",
            self.state, self.incoming, self.trash, self.backups
        ):
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise RepoError(f"unsafe repository directory: {directory}")
        for directory in (self.state, self.incoming, self.trash, self.backups):
            os.chmod(directory, 0o700)
        if self.packages.parent != self.arch_root:
            raise RepoError("invalid package directory layout")

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.prepare()
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def reindex(self) -> int:
        with self.locked():
            records = collect_records(self.packages)
            temporary = write_staged_index(self.index, records)
            publish_staged_index(temporary, self.index)
        return len(records)

    def status(self) -> int:
        with self.locked():
            records = collect_records(self.packages)
            expected = render_index(records)
            current = self.index.read_bytes() if self.index.is_file() and not self.index.is_symlink() else None
            package_bytes = sum((self.packages / record["filename"]).stat().st_size for record in records)
            mirrorlist_present = self.mirrorlist.is_file() and not self.mirrorlist.is_symlink()
        state = "current" if current == expected else "STALE or missing"
        print(f"Repository: {self.root}")
        print(f"Packages:   {len(records)}")
        print(f"Archive data: {package_bytes} bytes")
        print(f"Index:      {state}")
        print(f"Mirrorlist: {'present' if mirrorlist_present else 'missing'}")
        return 0 if current == expected else 1

    def list_packages(self) -> int:
        with self.locked():
            records = collect_records(self.packages)
        if not records:
            print("Repository is empty.")
            return 0
        print("NAME\tVERSION\tARCH\tFILENAME\tSHA256")
        for record in records:
            print(
                f"{record['name']}\t{record['version']}\t{record['arch']}\t"
                f"{record['filename']}\t{record['sha256']}"
            )
        return 0

    def list_recoverable(self) -> int:
        self.prepare()
        entries = []
        for label, directory in (("trash", self.trash), ("backup", self.backups)):
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if path.is_file() and not path.is_symlink():
                    entries.append((path.name, label, path.stat().st_size))
        if not entries:
            print("No recoverable package archives.")
            return 0
        print("ID\tTYPE\tSIZE")
        for entry_id, label, size in entries:
            print(f"{entry_id}\t{label}\t{size}")
        return 0

    def upload_batch(self, stream: BinaryIO, replace: bool) -> int:
        self.prepare()
        with tempfile.TemporaryDirectory(prefix="batch-", dir=self.incoming) as temporary_dir:
            incoming_dir = Path(temporary_dir)
            staged: dict[str, tuple[Path, dict[str, str]]] = {}
            total_bytes = 0
            try:
                with tarfile.open(fileobj=stream, mode="r|") as bundle:
                    for member in bundle:
                        if not member.isfile() or member.issym() or member.islnk():
                            raise RepoError("upload stream may contain regular package files only")
                        filename = member.name
                        if not safe_package_filename(filename):
                            raise RepoError(f"unsafe uploaded filename: {filename!r}")
                        if filename in staged:
                            raise RepoError(f"duplicate uploaded filename: {filename}")
                        if member.size <= 0 or member.size > MAX_PACKAGE_BYTES:
                            raise RepoError(f"invalid package size for {filename}: {member.size}")
                        total_bytes += member.size
                        if total_bytes > MAX_BATCH_BYTES:
                            raise RepoError(f"upload batch exceeds {MAX_BATCH_BYTES} bytes")

                        source = bundle.extractfile(member)
                        if source is None:
                            raise RepoError(f"cannot read uploaded member: {filename}")
                        target = incoming_dir / filename
                        remaining = member.size
                        with source, target.open("xb") as output:
                            while remaining:
                                chunk = source.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise RepoError(f"truncated upload stream for {filename}")
                                output.write(chunk)
                                remaining -= len(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        record = package_record(target, filename)
                        staged[filename] = (target, record)
            except (OSError, tarfile.TarError) as exc:
                raise RepoError(f"cannot read upload stream: {exc}") from exc

            if not staged:
                raise RepoError("upload batch contains no package archives")
            incoming_records = [record for _, record in staged.values()]
            validate_unique(incoming_records)

            with self.locked():
                existing_records = collect_records(self.packages)
                existing_by_filename = {record["filename"]: record for record in existing_records}
                unchanged: set[str] = set()
                replacements: set[str] = set()
                for filename, (_, record) in staged.items():
                    current = existing_by_filename.get(filename)
                    if current is None:
                        continue
                    if current["sha256"] == record["sha256"]:
                        unchanged.add(filename)
                    elif replace:
                        replacements.add(filename)
                    else:
                        raise RepoError(
                            f"{filename} already exists with different contents; "
                            "use upload --replace to replace it"
                        )

                effective_incoming = [
                    record for filename, (_, record) in staged.items()
                    if filename not in unchanged
                ]
                retained = [
                    record for record in existing_records
                    if record["filename"] not in replacements
                ]
                proposed = retained + effective_incoming
                validate_unique(proposed)

                temporary_index = write_staged_index(self.index, proposed)
                moved_to_backup: list[tuple[Path, Path]] = []
                installed: list[Path] = []
                try:
                    for filename in sorted(replacements):
                        source = self.packages / filename
                        backup = self.backups / timestamp_id(filename)
                        os.replace(source, backup)
                        moved_to_backup.append((backup, source))
                        print(f"Previous version backed up as: {backup.name}")
                    for filename, (source, _) in staged.items():
                        if filename in unchanged:
                            continue
                        destination = self.packages / filename
                        os.replace(source, destination)
                        installed.append(destination)
                        os.chmod(destination, 0o644)
                    publish_staged_index(temporary_index, self.index)
                    fsync_directory(self.packages)
                except Exception:
                    for path in reversed(installed):
                        path.unlink(missing_ok=True)
                    for backup, original in reversed(moved_to_backup):
                        if backup.exists():
                            os.replace(backup, original)
                    temporary_index.unlink(missing_ok=True)
                    raise

            added = len(staged) - len(unchanged) - len(replacements)
            print(f"Uploaded:   {added}")
            print(f"Replaced:   {len(replacements)}")
            print(f"Unchanged:  {len(unchanged)}")
            print(f"Repository: {len(proposed)} package(s)")
            if replacements:
                print("Previous versions are recoverable from the server backups directory.")
        return 0

    def delete_packages(self, filenames: list[str]) -> int:
        if not filenames:
            raise RepoError("specify at least one package filename")
        if len(set(filenames)) != len(filenames):
            raise RepoError("duplicate filename in deletion request")
        for filename in filenames:
            if not safe_package_filename(filename):
                raise RepoError(f"unsafe package filename: {filename!r}")

        with self.locked():
            records = collect_records(self.packages)
            by_filename = {record["filename"]: record for record in records}
            missing = [name for name in filenames if name not in by_filename]
            if missing:
                raise RepoError("package(s) not found: " + ", ".join(missing))

            removed_records = [by_filename[name] for name in filenames]
            retained = [record for record in records if record["filename"] not in set(filenames)]
            temporary_index = write_staged_index(self.index, retained)
            moved: list[tuple[Path, Path]] = []
            try:
                for filename in filenames:
                    source = self.packages / filename
                    trash_id = timestamp_id(filename)
                    destination = self.trash / trash_id
                    os.replace(source, destination)
                    moved.append((destination, source))
                    print(f"Moved to recoverable trash: {trash_id}")
                publish_staged_index(temporary_index, self.index)
                fsync_directory(self.packages)
            except Exception:
                for trash_path, original in reversed(moved):
                    if trash_path.exists():
                        os.replace(trash_path, original)
                temporary_index.unlink(missing_ok=True)
                raise

        print(f"Deleted from active repository: {len(removed_records)}")
        print(f"Packages remaining: {len(retained)}")
        return 0

    def restore_package(self, recoverable_id: str) -> int:
        if not TRASH_ID_RE.fullmatch(recoverable_id) or Path(recoverable_id).name != recoverable_id:
            raise RepoError(f"invalid recoverable ID: {recoverable_id!r}")
        trash_source = self.trash / recoverable_id
        backup_source = self.backups / recoverable_id
        if trash_source.is_file() and not trash_source.is_symlink():
            source = trash_source
            is_backup = False
        elif backup_source.is_file() and not backup_source.is_symlink():
            source = backup_source
            is_backup = True
        else:
            raise RepoError(f"recoverable package entry not found: {recoverable_id}")

        original_filename = recoverable_id.split("--", 1)[1]
        record = package_record(source, original_filename)

        with self.locked():
            destination = self.packages / original_filename
            records = collect_records(self.packages)
            existing = next(
                (item for item in records if item["filename"] == original_filename), None
            )
            if existing is not None and not is_backup:
                raise RepoError(f"package already exists in repository: {original_filename}")
            if existing is None and is_backup:
                raise RepoError(
                    f"cannot roll back backup because the active package is missing: {original_filename}"
                )

            proposed = [
                item for item in records if item["filename"] != original_filename
            ] + [record]
            validate_unique(proposed)
            temporary_index = write_staged_index(self.index, proposed)
            active_to_trash: tuple[Path, Path] | None = None
            try:
                if existing is not None:
                    displaced_id = timestamp_id(original_filename)
                    displaced = self.trash / displaced_id
                    os.replace(destination, displaced)
                    active_to_trash = (displaced, destination)
                os.replace(source, destination)
                os.chmod(destination, 0o644)
                publish_staged_index(temporary_index, self.index)
                fsync_directory(self.packages)
            except Exception:
                if destination.exists():
                    os.replace(destination, source)
                if active_to_trash is not None and active_to_trash[0].exists():
                    os.replace(active_to_trash[0], active_to_trash[1])
                temporary_index.unlink(missing_ok=True)
                raise

        if active_to_trash is not None:
            print(f"Replaced active package moved to trash: {active_to_trash[0].name}")
        print(f"Restored: {original_filename}")
        print(f"Packages in repository: {len(proposed)}")
        return 0

    def update_mirrorlist(self, stream: BinaryIO) -> int:
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RepoError("mirrorlist exceeds 1 MiB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepoError("mirrorlist is not UTF-8") from exc
        if "[mirror]" not in text or "https://" not in text:
            raise RepoError("mirrorlist must contain [mirror] and an HTTPS mirror URL")
        if any(char in text for char in "\x00"):
            raise RepoError("mirrorlist contains a NUL byte")

        self.prepare()
        with self.locked():
            self.mirrorlist.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".official.list.", suffix=".tmp", dir=self.mirrorlist.parent
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary_path, 0o644)
                os.replace(temporary_path, self.mirrorlist)
                fsync_directory(self.mirrorlist.parent)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
        print(f"Updated mirrorlist: {self.mirrorlist}")
        return 0


def ssh_command(host: str, server_command: str, *server_args: str) -> list[str]:
    remote = shlex.join([
        "doas", server_command, "server", *server_args
    ])
    return ["ssh", host, remote]


def run_remote(host: str, server_command: str, *server_args: str,
               input_data: bytes | None = None) -> int:
    command = ssh_command(host, server_command, *server_args)
    try:
        result = subprocess.run(command, input=input_data, check=False)
    except FileNotFoundError as exc:
        raise RepoError(f"required client tool is unavailable: {exc.filename}") from exc
    return result.returncode


def client_upload(host: str, server_command: str, paths: list[Path], replace: bool) -> int:
    if not paths:
        raise RepoError("specify at least one .khmpkg file")
    records: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise RepoError(f"package is missing or unsafe: {path}")
        if not safe_package_filename(path.name):
            raise RepoError(f"invalid package filename: {path.name}")
        if path.name in seen_names:
            raise RepoError(f"duplicate package input: {path.name}")
        seen_names.add(path.name)
        record = package_record(path)
        records.append(record)
        print(
            f"Validated {record['name']} {record['version']} "
            f"({record['arch']}): {path.name}",
            flush=True,
        )
    validate_unique(records)

    server_args = ["upload-batch"]
    if replace:
        server_args.append("--replace")
    command = ssh_command(host, server_command, *server_args)
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RepoError(f"required client tool is unavailable: {exc.filename}") from exc

    try:
        assert process.stdin is not None
        with tarfile.open(fileobj=process.stdin, mode="w|") as bundle:
            for path in paths:
                info = tarfile.TarInfo(path.name)
                info.size = path.stat().st_size
                info.mode = 0o600
                with path.open("rb") as source:
                    bundle.addfile(info, source)
        process.stdin.close()
        returncode = process.wait()
    except Exception:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.terminate()
        process.wait()
        raise
    return returncode


def client_delete(host: str, server_command: str, filenames: list[str], assume_yes: bool) -> int:
    if not filenames:
        raise RepoError("specify package filenames to delete")
    if not assume_yes:
        print("The following packages will be removed from the active repository:")
        for filename in filenames:
            print(f"  {filename}")
        confirmation = input("Type DELETE to move them to recoverable trash: ")
        if confirmation != "DELETE":
            print("Deletion cancelled.")
            return 0
    return run_remote(host, server_command, "delete", "--yes", *filenames)


def client_mirrorlist(host: str, server_command: str, path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise RepoError(f"mirrorlist is missing or unsafe: {path}")
    raw = path.read_bytes()
    return run_remote(host, server_command, "mirrorlist", input_data=raw)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the KhaymOS package repository over SSH."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH host alias (default: khaymos-repo)")
    parser.add_argument(
        "--server-command", default=DEFAULT_SERVER_COMMAND,
        help="installed server-side command path"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="validate the remote repository and index")
    commands.add_parser("list", help="list published packages")

    upload = commands.add_parser("upload", help="validate and upload one or more packages")
    upload.add_argument("--replace", action="store_true", help="back up and replace matching filenames")
    upload.add_argument("packages", nargs="+", type=Path)

    delete = commands.add_parser("delete", help="move published packages into recoverable trash")
    delete.add_argument("--yes", action="store_true", help="skip the local confirmation prompt")
    delete.add_argument("filenames", nargs="+")

    restore = commands.add_parser(
        "restore", help="restore a deleted package or roll back a replacement"
    )
    restore.add_argument("trash_id", help="ID shown by 'khrepo trash' or an upload backup")

    commands.add_parser("reindex", help="validate all archives and regenerate index.tsv")
    commands.add_parser("trash", help="list deleted packages and replaced versions available to restore")

    mirrorlist = commands.add_parser("mirrorlist", help="publish config/official.mirrorlist")
    mirrorlist.add_argument("path", nargs="?", type=Path, default=Path("config/official.mirrorlist"))

    server = commands.add_parser("server", help=argparse.SUPPRESS)
    server.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    server_commands = server.add_subparsers(dest="server_command", required=True)
    server_commands.add_parser("status")
    server_commands.add_parser("list")
    server_commands.add_parser("reindex")
    server_commands.add_parser("trash")

    upload_server = server_commands.add_parser("upload-batch")
    upload_server.add_argument("--replace", action="store_true")

    delete_server = server_commands.add_parser("delete")
    delete_server.add_argument("--yes", action="store_true", required=True)
    delete_server.add_argument("filenames", nargs="+")

    restore_server = server_commands.add_parser("restore")
    restore_server.add_argument("trash_id")

    server_commands.add_parser("mirrorlist")
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()

    try:
        if args.command == "server":
            if os.geteuid() != 0:
                raise RepoError("server operations must run as root (use doas)")
            repo = Repository(args.repo_root)
            if args.server_command == "status":
                return repo.status()
            if args.server_command == "list":
                return repo.list_packages()
            if args.server_command == "reindex":
                count = repo.reindex()
                print(f"Repository index rebuilt: {count} package(s)")
                return 0
            if args.server_command == "trash":
                return repo.list_recoverable()
            if args.server_command == "upload-batch":
                return repo.upload_batch(sys.stdin.buffer, args.replace)
            if args.server_command == "delete":
                return repo.delete_packages(args.filenames)
            if args.server_command == "restore":
                return repo.restore_package(args.trash_id)
            if args.server_command == "mirrorlist":
                return repo.update_mirrorlist(sys.stdin.buffer)
            raise RepoError("unknown server operation")

        if not args.host:
            raise RepoError("SSH host is empty")
        if args.command == "status":
            return run_remote(args.host, args.server_command, "status")
        if args.command == "list":
            return run_remote(args.host, args.server_command, "list")
        if args.command == "upload":
            return client_upload(args.host, args.server_command, args.packages, args.replace)
        if args.command == "delete":
            return client_delete(args.host, args.server_command, args.filenames, args.yes)
        if args.command == "restore":
            return run_remote(args.host, args.server_command, "restore", args.trash_id)
        if args.command == "reindex":
            return run_remote(args.host, args.server_command, "reindex")
        if args.command == "trash":
            return run_remote(args.host, args.server_command, "trash")
        if args.command == "mirrorlist":
            return client_mirrorlist(args.host, args.server_command, args.path)
        raise RepoError("unknown command")
    except (OSError, RepoError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"khrepo: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
