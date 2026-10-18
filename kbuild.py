#!/usr/bin/env python3
"""KBuild v0.4 — KhaymOS Build System.

Recovery build of KBuild v0.4 based on the surviving v0.3 source and the
project's documented v0.4 design. It preserves v0.3's recipe/fetch/build flow
and adds host/target build types, target sysroot deployment, ownership tracking,
and promotion of host-built cross-toolchain packages.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


VERSION = "0.4"
ROOT = Path(__file__).resolve().parent
RECIPES = ROOT / "recipes"
CACHE = ROOT / "cache"
METADATA = CACHE / "metadata"
WORK = ROOT / "work"
LOGS = ROOT / "logs"
DESTDIR_ROOT = ROOT / "destdir"
SYSROOT = ROOT / "sysroot"
TOOLCHAIN = ROOT / "toolchain"

# The toolchain must be built in stages: native binutils, Linux API headers,
# a libc-less stage-1 compiler, target libc, and finally the full cross-GCC.
BOOTSTRAP_ORDER = (
    "binutils-cross",
    "linux-headers",
    "gcc-cross-stage1",
    "glibc",
    "gcc-cross",
)
BOOTSTRAP_ACTIONS = {
    "binutils-cross": "build and promote into toolchain/",
    "linux-headers": "build with host tools and deploy into sysroot/",
    "gcc-cross-stage1": "build and promote into toolchain/bootstrap/",
    "glibc": "build with the bootstrap compiler and deploy into sysroot/",
    "gcc-cross": "build and promote the final cross-compiler into toolchain/",
}

TARGET = os.environ.get("KBUILD_TARGET", "x86_64-khaymos-linux-gnu")
GENERIC_CFLAGS = "-O2 -pipe -march=x86-64 -mtune=generic"
USER_AGENT = f"KBuild/{VERSION}"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SYSROOT_OWNERSHIP = SYSROOT / ".kbuild-ownership.json"
TOOLCHAIN_OWNERSHIP = TOOLCHAIN / ".kbuild-ownership.json"


class KBuildError(Exception):
    """Expected build-system error."""


def ensure_directories():
    for directory in (
        RECIPES, CACHE, METADATA, WORK, LOGS, DESTDIR_ROOT, SYSROOT, TOOLCHAIN
    ):
        directory.mkdir(parents=True, exist_ok=True)


def package_name(name):
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise KBuildError(f"Invalid package name: {name!r}")
    return name


def archive_path(url):
    filename = Path(urllib.parse.urlparse(url).path).name
    if not filename or filename in (".", ".."):
        raise KBuildError(f"Cannot determine archive filename from {url!r}")
    return filename


def _read_recipe(name):
    name = package_name(name)
    recipe_path = RECIPES / f"{name}.toml"
    if not recipe_path.is_file():
        raise KBuildError(f"Recipe not found: {recipe_path}")

    try:
        with recipe_path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise KBuildError(f"Cannot read recipe {name}: {exc}") from exc

    if data.get("name") != name:
        raise KBuildError(
            f"Recipe name mismatch: expected {name!r}, got {data.get('name')!r}"
        )

    for field in ("version", "source", "sha256"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise KBuildError(f"{name}: missing or invalid {field!r}")

    parsed_url = urllib.parse.urlparse(data["source"])
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise KBuildError(f"{name}: source must be a valid HTTPS URL")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", data["sha256"]):
        raise KBuildError(f"{name}: sha256 must contain 64 hexadecimal digits")

    for field in ("build", "install", "depends"):
        value = data.get(field, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise KBuildError(f"{name}: {field} must be an array of non-empty strings")

    environment = data.get("environment", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise KBuildError(f"{name}: environment must be a string-to-string table")
    for key in environment:
        if not ENV_NAME_PATTERN.fullmatch(key):
            raise KBuildError(f"{name}: invalid environment variable {key!r}")

    dependencies = data.get("depends", [])
    if len(dependencies) != len(set(dependencies)):
        raise KBuildError(f"{name}: duplicate dependencies are not allowed")
    for dependency in dependencies:
        package_name(dependency)
        if dependency == name:
            raise KBuildError(f"{name}: package cannot depend on itself")

    archive_root = data.get("archive_root")
    if archive_root is not None and (
        not isinstance(archive_root, str)
        or not archive_root
        or archive_root in (".", "..")
        or "/" in archive_root
        or "\\" in archive_root
    ):
        raise KBuildError(f"{name}: invalid archive_root")

    build_type = data.get("build_type", "host")
    if build_type not in ("host", "target"):
        raise KBuildError(f"{name}: build_type must be 'host' or 'target'")
    data["build_type"] = build_type

    for field in ("requires_cross_compiler", "allow_bootstrap_compiler"):
        if field in data and not isinstance(data[field], bool):
            raise KBuildError(f"{name}: {field} must be a boolean")
    if data.get("allow_bootstrap_compiler", False) and build_type != "target":
        raise KBuildError(
            f"{name}: allow_bootstrap_compiler is only valid for target recipes"
        )
    return data


def _validate_dependency_graph(name, trail=None, checked=None):
    trail = [] if trail is None else list(trail)
    checked = set() if checked is None else checked
    if name in trail:
        raise KBuildError("Dependency cycle detected: " + " -> ".join(trail + [name]))
    if name in checked:
        return
    data = _read_recipe(name)
    next_trail = trail + [name]
    for dependency in data.get("depends", []):
        _validate_dependency_graph(dependency, next_trail, checked)
    checked.add(name)


def load_recipe(name):
    data = _read_recipe(name)
    _validate_dependency_graph(name)
    return data


def package_paths(data):
    package_id = f"{data['name']}-{data['version']}"
    return {
        "source": CACHE / archive_path(data["source"]),
        "work": WORK / package_id,
        "stage": DESTDIR_ROOT / package_id,
        "log": LOGS / f"{package_id}.log",
        "metadata": METADATA / f"{package_id}.json",
    }


def package_id(data):
    return f"{data['name']}-{data['version']}"


def recipe_fingerprint(data, dependency_fingerprints):
    """Hash recipe text, compiler settings, build type, target and dependencies."""
    recipe_path = RECIPES / f"{data['name']}.toml"
    digest = hashlib.sha256()
    digest.update(b"KBuild-fingerprint-v4\0")
    digest.update(recipe_path.read_bytes())
    digest.update(b"\0target\0")
    digest.update(TARGET.encode())
    digest.update(b"\0build-type\0")
    digest.update(data.get("build_type", "host").encode())
    digest.update(b"\0cflags\0")
    digest.update(GENERIC_CFLAGS.encode())
    digest.update(b"\0cxxflags\0")
    digest.update(GENERIC_CFLAGS.encode())
    for dependency, fingerprint in sorted(dependency_fingerprints.items()):
        digest.update(b"\0dependency\0")
        digest.update(dependency.encode())
        digest.update(b"\0")
        digest.update(fingerprint.encode())
    return digest.hexdigest()


def fetch(data):
    paths = package_paths(data)
    destination = paths["source"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        print(f"Cached: {destination.name}")
        return destination

    request = urllib.request.Request(data["source"], headers={"User-Agent": USER_AGENT})
    temporary = destination.with_name(destination.name + ".part")
    print(f"Downloading {data['source']}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
        temporary.replace(destination)
    except (OSError, urllib.error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise KBuildError(f"Download failed for {data['name']}: {exc}") from exc
    print(destination)
    return destination


def verify(path, expected):
    if not path.is_file():
        raise KBuildError(f"Source archive not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    expected = expected.lower()
    if actual != expected:
        raise KBuildError(
            f"SHA-256 mismatch for {path.name}\n"
            f"Expected: {expected}\nActual:   {actual}\n"
            "The archive will not be built."
        )
    print(f"SHA-256 OK: {path.name}")
    return actual


def safe_extract(archive, destination):
    """Reject archive traversal, escaping links, and special files before extract."""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise KBuildError(
                    f"Unsafe archive path in {archive.name}: {member.name!r}"
                )
            resolved = (destination / member.name).resolve()
            if not resolved.is_relative_to(destination):
                raise KBuildError(f"Archive path escapes destination: {member.name!r}")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise KBuildError(f"Unsupported archive entry: {member.name!r}")
            if member.issym() or member.islnk():
                link = Path(member.linkname)
                if link.is_absolute():
                    raise KBuildError(f"Absolute archive link rejected: {member.name!r}")
                link_target = ((resolved.parent / link) if member.issym()
                               else (destination / link)).resolve()
                if not link_target.is_relative_to(destination):
                    raise KBuildError(f"Archive link escapes destination: {member.name!r}")
        try:
            tar.extractall(destination, members=members, filter="data")
        except TypeError:
            # Compatibility for Python versions without tarfile's data filter;
            # all member paths and links were checked above.
            tar.extractall(destination, members=members)


def run_commands(commands, cwd, environment, log_path, phase):
    if not commands:
        return
    with log_path.open("a", encoding="utf-8") as log:
        for index, command in enumerate(commands, start=1):
            label = f"[{phase} {index}/{len(commands)}] {command}"
            print(label)
            log.write(f"\n$ {label}\n")
            log.flush()
            try:
                result = subprocess.run(
                    ["/bin/bash", "-e", "-o", "pipefail", "-c", command],
                    cwd=cwd,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            except OSError as exc:
                raise KBuildError(f"Could not execute {phase} command: {exc}") from exc
            if result.returncode != 0:
                raise KBuildError(
                    f"{phase} command failed with exit code {result.returncode}: {command}\n"
                    f"See log: {log_path}"
                )


def read_metadata(path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            metadata = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return None
    return metadata if isinstance(metadata, dict) else None


def is_cached(data, fingerprint):
    """Return whether a matching build has a non-empty staging tree."""
    paths = package_paths(data)
    metadata = read_metadata(paths["metadata"])
    if metadata is None or not paths["stage"].is_dir():
        return False
    if (
        metadata.get("name") != data["name"]
        or metadata.get("version") != data["version"]
        or metadata.get("fingerprint") != fingerprint
        or metadata.get("target") != TARGET
        or metadata.get("build_type", "host") != data.get("build_type", "host")
    ):
        return False
    return any(paths["stage"].iterdir())


def is_deployed(data, fingerprint):
    """Check whether a matching target package is still deployed in the sysroot."""
    if data.get("build_type", "host") != "target":
        return False

    metadata = read_metadata(package_paths(data)["metadata"])
    if metadata is None:
        return False

    if (
        metadata.get("name") != data["name"]
        or metadata.get("version") != data["version"]
        or metadata.get("fingerprint") != fingerprint
        or metadata.get("target") != TARGET
        or metadata.get("build_type", "host") != "target"
    ):
        return False

    database = _read_ownership(SYSROOT_OWNERSHIP, TARGET)
    owned_paths = database["packages"].get(package_id(data), [])
    if not owned_paths:
        return False

    for entry in owned_paths:
        relative = Path(_safe_relative(entry))
        destination = SYSROOT / relative
        if not destination.exists() and not destination.is_symlink():
            return False

    return True


def save_metadata(data, fingerprint, dependency_fingerprints):
    paths = package_paths(data)
    paths["metadata"].parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "format": 2,
        "kbuild_version": VERSION,
        "name": data["name"],
        "version": data["version"],
        "target": TARGET,
        "build_type": data.get("build_type", "host"),
        "fingerprint": fingerprint,
        "dependencies": dict(sorted(dependency_fingerprints.items())),
    }
    temporary = paths["metadata"].with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(paths["metadata"])


def target_compiler_path(data):
    """Find a usable target compiler, honoring explicit bootstrap opt-in."""
    if data.get("allow_bootstrap_compiler", False):
        bootstrap = TOOLCHAIN / "bootstrap" / "bin" / f"{TARGET}-gcc"
        if bootstrap.is_file() and os.access(bootstrap, os.X_OK):
            return bootstrap

    compiler = TOOLCHAIN / "bin" / f"{TARGET}-gcc"
    if compiler.is_file() and os.access(compiler, os.X_OK):
        return compiler
    return None


def build_environment(data, stage):
    build_type = data.get("build_type", "host")
    environment = os.environ.copy()
    environment.update({
        "KBUILD_TOOLCHAIN": str(TOOLCHAIN.resolve()),
        "KBUILD_VERSION": VERSION,
        "KBUILD_PACKAGE": data["name"],
        "KBUILD_PACKAGE_VERSION": data["version"],
        "KBUILD_TARGET": TARGET,
        "KBUILD_BUILD_TYPE": build_type,
        "SYSROOT": str(SYSROOT.resolve()),
        "DESTDIR": str(stage),
        "PREFIX": "/usr",
        "CFLAGS": GENERIC_CFLAGS,
        "CXXFLAGS": GENERIC_CFLAGS,
        "LC_ALL": "C",
        "TZ": "UTC",
    })

    if build_type == "target":
        bin_dir = TOOLCHAIN / "bin"
        compiler = target_compiler_path(data)
        if compiler is None:
            if data.get("requires_cross_compiler", True):
                expected = bin_dir / f"{TARGET}-gcc"
                raise KBuildError(
                    f"{data['name']}: target build requires executable cross-compiler:\n"
                    f"  {expected}\n"
                    "Build and promote the bootstrap/final KhaymOS cross-GCC first."
                )

            # A recipe explicitly marked as not requiring the cross compiler
            # (currently linux-headers) uses host tools only to export headers.
            environment.update({
                "CC": os.environ.get("CC", "cc"),
                "CXX": os.environ.get("CXX", "c++"),
                "AR": os.environ.get("AR", "ar"),
                "AS": os.environ.get("AS", "as"),
                "LD": os.environ.get("LD", "ld"),
                "NM": os.environ.get("NM", "nm"),
                "RANLIB": os.environ.get("RANLIB", "ranlib"),
                "STRIP": os.environ.get("STRIP", "strip"),
                "CFLAGS": GENERIC_CFLAGS,
                "CXXFLAGS": GENERIC_CFLAGS,
                "LDFLAGS": os.environ.get("LDFLAGS", ""),
            })
        else:
            cxx_compiler = compiler.parent / f"{TARGET}-g++"
            if not cxx_compiler.is_file() or not os.access(cxx_compiler, os.X_OK):
                # C-only packages may not need C++; don't force CC recipes to use it.
                cxx_value = f"{TARGET}-g++"
            else:
                cxx_value = str(cxx_compiler)
            environment.update({
                "CC": str(compiler),
                "CXX": cxx_value,
                "AR": str(bin_dir / f"{TARGET}-ar"),
                "AS": str(bin_dir / f"{TARGET}-as"),
                "LD": str(bin_dir / f"{TARGET}-ld"),
                "NM": str(bin_dir / f"{TARGET}-nm"),
                "RANLIB": str(bin_dir / f"{TARGET}-ranlib"),
                "STRIP": str(bin_dir / f"{TARGET}-strip"),
                "CFLAGS": f"{GENERIC_CFLAGS} --sysroot={SYSROOT}",
                "CXXFLAGS": f"{GENERIC_CFLAGS} --sysroot={SYSROOT}",
                "LDFLAGS": f"--sysroot={SYSROOT}",
                "PKG_CONFIG_SYSROOT_DIR": str(SYSROOT),
                "PKG_CONFIG_LIBDIR": os.pathsep.join((
                    str(SYSROOT / "usr/lib/pkgconfig"),
                    str(SYSROOT / "usr/share/pkgconfig"),
                    str(SYSROOT / "usr/lib64/pkgconfig"),
                )),
            })
    else:
        # Host recipes use the native compiler unless the recipe overrides it.
        environment.setdefault("CC", os.environ.get("CC", "cc"))
        environment.setdefault("CXX", os.environ.get("CXX", "c++"))

    for key, value in data.get("environment", {}).items():
        if not ENV_NAME_PATTERN.fullmatch(key):
            raise KBuildError(f"{data['name']}: invalid environment variable {key!r}")
        environment[key] = value
    return environment


def build(name, stack=None, force=False, as_dependency=False):
    name = package_name(name)
    stack = [] if stack is None else list(stack)
    if name in stack:
        raise KBuildError("Dependency cycle detected: " + " -> ".join(stack + [name]))

    data = load_recipe(name)
    paths = package_paths(data)
    dependency_fingerprints = {}
    next_stack = stack + [name]
    for dependency in data.get("depends", []):
        print(f"[dependency] {name} requires {dependency}")
        dependency_fingerprints[dependency] = build(
            dependency, next_stack, force=False, as_dependency=True
        )

    fingerprint = recipe_fingerprint(data, dependency_fingerprints)
    if not force:
        if is_cached(data, fingerprint):
            print(f"Up to date: {name} {data['version']}")
            return fingerprint

        if as_dependency and is_deployed(data, fingerprint):
            print(f"Using deployed dependency: {name} {data['version']}")
            return fingerprint

    # Fail before downloading or compiling target packages without a usable
    # compiler. Explicit host-tools-only recipes and the glibc bootstrap recipe
    # are the only exceptions.
    if (
        data.get("build_type", "host") == "target"
        and data.get("requires_cross_compiler", True)
        and target_compiler_path(data) is None
    ):
        compiler = TOOLCHAIN / "bin" / f"{TARGET}-gcc"
        raise KBuildError(
            f"{name}: target build requires {compiler}; run 'build all' "
            "to perform the toolchain bootstrap, or promote the compiler first"
        )

    archive = fetch(data)
    verify(archive, data["sha256"])
    shutil.rmtree(paths["work"], ignore_errors=True)
    shutil.rmtree(paths["stage"], ignore_errors=True)
    paths["work"].mkdir(parents=True, exist_ok=True)
    paths["stage"].mkdir(parents=True, exist_ok=True)
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    paths["log"].write_text(
        f"KBuild {VERSION}\nPackage: {name} {data['version']}\n"
        f"Target: {TARGET}\nBuild type: {data.get('build_type', 'host')}\n"
        f"Fingerprint: {fingerprint}\n",
        encoding="utf-8",
    )

    try:
        safe_extract(archive, paths["work"])
        archive_root = data.get("archive_root")
        if archive_root:
            source_dir = paths["work"] / archive_root
            if not source_dir.is_dir():
                raise KBuildError(
                    f"{name}: expected archive root not found: {archive_root}"
                )
        else:
            entries = list(paths["work"].iterdir())
            source_dir = entries[0] if len(entries) == 1 and entries[0].is_dir() else paths["work"]

        environment = build_environment(data, paths["stage"])
        run_commands(data.get("build", []), source_dir, environment, paths["log"], "build")
        run_commands(data.get("install", []), source_dir, environment, paths["log"], "install")
        if not any(paths["stage"].iterdir()):
            raise KBuildError(f"{name}: installation produced an empty staging directory")
        save_metadata(data, fingerprint, dependency_fingerprints)
    except Exception:
        paths["metadata"].unlink(missing_ok=True)
        raise

    print(f"Built: {name} {data['version']}")
    print(f"Staged at: {paths['stage']}")
    print(f"Log: {paths['log']}")
    print(f"Metadata: {paths['metadata']}")
    return fingerprint


def clean(name=None, all_files=False):
    if all_files:
        for directory in (WORK, LOGS, DESTDIR_ROOT, METADATA):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)
        print("Removed build work, logs, staging trees, and build metadata.")
        print("Downloaded source archives, the sysroot, and toolchain were preserved.")
        return
    if name is None:
        raise KBuildError("Specify a package name or use clean --all")
    data = load_recipe(name)
    paths = package_paths(data)
    for key in ("work", "stage", "log", "metadata"):
        path = paths[key]
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    print(f"Cleaned build output for {name} {data['version']}.")
    print("Downloaded source archives, sysroot deployments, and toolchain were preserved.")


def staged_entries(stage):
    """Return stage entries as paths relative to stage, including directories."""
    stage = stage.resolve()
    entries = []
    for path in stage.rglob("*"):
        # rglob does not follow directory symlinks; lstat preserves symlink identity.
        try:
            path.relative_to(stage)
            path.lstat()
        except (OSError, ValueError) as exc:
            raise KBuildError(f"Cannot inspect staged entry {path}: {exc}") from exc
        entries.append(path.relative_to(stage))
    return sorted(entries, key=lambda p: (len(p.parts), p.as_posix()))


def _entry_kind(path):
    mode = path.lstat().st_mode
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    raise KBuildError(f"Unsupported staged filesystem entry: {path}")


def _safe_relative(relative):
    text = relative.as_posix() if isinstance(relative, Path) else str(relative)
    path = Path(text)
    if path.is_absolute() or not text or text == "." or ".." in path.parts:
        raise KBuildError(f"Unsafe relative path in package ownership data: {text!r}")
    return path.as_posix()


def _read_ownership(path, target_label):
    if not path.exists():
        return {"format": 1, "target": target_label, "packages": {}}
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise KBuildError(f"Cannot read ownership database {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("packages"), dict):
        raise KBuildError(f"Invalid ownership database format: {path}")
    if data.get("target") not in (None, target_label):
        raise KBuildError(
            f"Ownership database {path} is for target {data.get('target')!r}, "
            f"not {target_label!r}"
        )
    # Validate loaded paths before using them on disk.
    for owner, entries in data["packages"].items():
        if not isinstance(owner, str) or not isinstance(entries, list):
            raise KBuildError(f"Invalid ownership entry in {path}")
        for entry in entries:
            _safe_relative(entry)
    data["format"] = 1
    data["target"] = target_label
    return data


def _write_ownership(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(path)


def _owners_by_path(database):
    owners = {}
    for owner, entries in database["packages"].items():
        for relative in entries:
            owners.setdefault(relative, set()).add(owner)
    return owners


def _check_destination_parents(root, relative):
    """Reject symlinked parent directories so installation cannot escape its root."""
    current = root
    for part in Path(relative).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise KBuildError(f"Refusing to install through symlinked parent: {current}")
        if current.exists() and not current.is_dir():
            raise KBuildError(f"Destination parent is not a directory: {current}")


def _copy_entry(source, destination, kind):
    if kind == "dir":
        if destination.exists() and not destination.is_dir():
            raise KBuildError(f"Cannot replace non-directory with directory: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copystat(source, destination, follow_symlinks=False)
        except OSError:
            pass
    elif kind == "symlink":
        if destination.is_dir() and not destination.is_symlink():
            raise KBuildError(f"Cannot replace directory with symlink: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                raise KBuildError(f"Cannot replace directory with symlink: {destination}")
            destination.unlink()
        os.symlink(os.readlink(source), destination)
    else:
        if destination.is_dir() and not destination.is_symlink():
            raise KBuildError(f"Cannot replace directory with file: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                raise KBuildError(f"Cannot replace directory with file: {destination}")
            destination.unlink()
        shutil.copy2(source, destination, follow_symlinks=False)


def _require_built(data):
    paths = package_paths(data)
    metadata = read_metadata(paths["metadata"])
    if metadata is None or not paths["stage"].is_dir() or not any(paths["stage"].iterdir()):
        raise KBuildError(
            f"No successful staged build found for {data['name']} {data['version']}; "
            "run 'build' first."
        )
    if metadata.get("name") != data["name"] or metadata.get("version") != data["version"]:
        raise KBuildError(f"Build metadata does not match recipe for {data['name']}")
    recorded_type = metadata.get("build_type", "host")
    if recorded_type != data.get("build_type", "host"):
        raise KBuildError(
            f"Build metadata says build_type={recorded_type!r}, but recipe says "
            f"{data.get('build_type', 'host')!r}; rebuild the package."
        )
    if metadata.get("target") != TARGET:
        raise KBuildError(f"Build metadata target does not match current target {TARGET}")
    return paths, metadata


def deploy(name):
    """Deploy a successfully built target package to the sysroot."""
    data = load_recipe(name)
    if data.get("build_type", "host") != "target":
        raise KBuildError(f"{name}: only build_type='target' packages can be deployed")
    paths, metadata = _require_built(data)
    stage = paths["stage"]
    entries = staged_entries(stage)
    if not entries:
        raise KBuildError(f"{name}: staging directory is empty")

    database = _read_ownership(SYSROOT_OWNERSHIP, TARGET)
    owner_id = package_id(data)
    old_entries = set(database["packages"].get(owner_id, []))
    owners = _owners_by_path(database)
    payload_paths = []

    # Preflight the complete payload before modifying the sysroot.
    for relative in entries:
        relative_text = _safe_relative(relative)
        source = stage / relative
        kind = _entry_kind(source)
        destination = SYSROOT / relative
        _check_destination_parents(SYSROOT, relative_text)
        if kind == "dir":
            if destination.exists() and not destination.is_dir():
                raise KBuildError(f"Deployment collision at {destination}")
            continue
        other_owners = owners.get(relative_text, set()) - {owner_id}
        if other_owners:
            raise KBuildError(
                f"Deployment collision: {relative_text} is owned by "
                + ", ".join(sorted(other_owners))
            )
        if (destination.exists() or destination.is_symlink()) and relative_text not in old_entries:
            raise KBuildError(
                f"Deployment would overwrite an unowned sysroot entry: {destination}"
            )
        payload_paths.append(relative_text)

    # Copy directories before their descendants, then leaf entries.
    try:
        for relative in entries:
            source = stage / relative
            destination = SYSROOT / relative
            _copy_entry(source, destination, _entry_kind(source))
    except Exception:
        # Keep the ownership database unchanged. Any new files from a rare I/O
        # failure are left visible rather than deleting unknown pre-existing data.
        raise

    database["packages"][owner_id] = sorted(set(payload_paths))
    _write_ownership(SYSROOT_OWNERSHIP, database)
    print(f"Deployed: {owner_id}")
    print(f"Sysroot: {SYSROOT}")
    print(f"Ownership: {SYSROOT_OWNERSHIP}")


def undeploy(name):
    """Remove only files recorded as owned by this package."""
    data = load_recipe(name)
    owner_id = package_id(data)
    database = _read_ownership(SYSROOT_OWNERSHIP, TARGET)
    entries = database["packages"].get(owner_id)
    if entries is None:
        raise KBuildError(f"No sysroot deployment recorded for {owner_id}")

    for relative_text in sorted(entries, key=lambda value: (len(Path(value).parts), value), reverse=True):
        relative = Path(_safe_relative(relative_text))
        destination = SYSROOT / relative
        _check_destination_parents(SYSROOT, relative_text)
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            destination.unlink(missing_ok=True)
    del database["packages"][owner_id]
    _write_ownership(SYSROOT_OWNERSHIP, database)

    # Remove now-empty parent directories, stopping at the sysroot root.
    for relative_text in sorted(entries, key=lambda value: len(Path(value).parts), reverse=True):
        current = (SYSROOT / Path(relative_text)).parent
        while current != SYSROOT and current.is_relative_to(SYSROOT):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
    print(f"Undeployed: {owner_id}")


def promote(name):
    """Promote a host-built package installed under KBUILD_TOOLCHAIN into toolchain/."""
    data = load_recipe(name)
    if data.get("build_type", "host") != "host":
        raise KBuildError(f"{name}: only host-built packages can be promoted")
    paths, metadata = _require_built(data)
    stage = paths["stage"].resolve()
    toolchain_prefix = TOOLCHAIN.resolve()
    prefix_parts = toolchain_prefix.parts[1:] if toolchain_prefix.is_absolute() else toolchain_prefix.parts
    staged_prefix = stage.joinpath(*prefix_parts)
    if not staged_prefix.is_dir():
        raise KBuildError(
            f"{name}: staged output does not contain KBUILD_TOOLCHAIN prefix:\n"
            f"  Expected: {staged_prefix}\n"
            "Check that the recipe uses --prefix=\"$KBUILD_TOOLCHAIN\"."
        )

    all_entries = staged_entries(stage)
    accepted_ancestors = {Path(*prefix_parts[:index]) for index in range(1, len(prefix_parts) + 1)}
    for relative in all_entries:
        source = stage / relative
        kind = _entry_kind(source)
        if relative == Path(*prefix_parts) or Path(*prefix_parts).is_relative_to(relative):
            # This is the prefix directory itself or a parent directory.
            if kind != "dir":
                raise KBuildError(f"Unexpected non-directory above toolchain prefix: {source}")
            continue
        try:
            relative.relative_to(Path(*prefix_parts))
        except ValueError:
            # Any payload outside the configured prefix is unexpected.
            if kind != "dir" or relative not in accepted_ancestors:
                raise KBuildError(
                    f"{name}: refusing to promote package with staged paths outside "
                    f"KBUILD_TOOLCHAIN: {relative}"
                )

    payload_entries = staged_entries(staged_prefix)
    payload_files = []
    if not payload_entries:
        raise KBuildError(f"{name}: no files found under staged toolchain prefix")
    database = _read_ownership(TOOLCHAIN_OWNERSHIP, TARGET)
    owner_id = package_id(data)
    old_entries = set(database["packages"].get(owner_id, []))
    owners = _owners_by_path(database)

    for relative in payload_entries:
        relative_text = _safe_relative(relative)
        source = staged_prefix / relative
        kind = _entry_kind(source)
        destination = TOOLCHAIN / relative
        _check_destination_parents(TOOLCHAIN, relative_text)
        if kind == "dir":
            if destination.exists() and not destination.is_dir():
                raise KBuildError(f"Toolchain promotion collision at {destination}")
            continue
        other_owners = owners.get(relative_text, set()) - {owner_id}
        if other_owners:
            raise KBuildError(
                f"Toolchain collision: {relative_text} is owned by "
                + ", ".join(sorted(other_owners))
            )
        if (destination.exists() or destination.is_symlink()) and relative_text not in old_entries:
            raise KBuildError(
                f"Promotion would overwrite an unowned toolchain entry: {destination}"
            )
        payload_files.append(relative_text)

    new_payload_paths = set(payload_files)
    stale_entries = old_entries - new_payload_paths
    for relative_text in stale_entries:
        other_owners = owners.get(relative_text, set()) - {owner_id}
        if other_owners:
            raise KBuildError(
                f"Cannot replace stale toolchain entry {relative_text}; also owned by "
                + ", ".join(sorted(other_owners))
            )

    for relative in payload_entries:
        source = staged_prefix / relative
        destination = TOOLCHAIN / relative
        _copy_entry(source, destination, _entry_kind(source))

    # A rebuilt toolchain package may no longer install every path from its
    # previous version (for example, moving GCC into toolchain/bootstrap/).
    # Remove only paths that this same package previously owned.
    for relative_text in sorted(
        stale_entries,
        key=lambda value: (len(Path(value).parts), value),
        reverse=True,
    ):
        destination = TOOLCHAIN / Path(_safe_relative(relative_text))
        if destination.is_symlink() or (
            destination.exists() and not destination.is_dir()
        ):
            destination.unlink()
    for relative_text in sorted(
        stale_entries,
        key=lambda value: (len(Path(value).parts), value),
        reverse=True,
    ):
        current = (TOOLCHAIN / Path(_safe_relative(relative_text))).parent
        while current != TOOLCHAIN and current.is_relative_to(TOOLCHAIN):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    database["packages"][owner_id] = sorted(new_payload_paths)
    _write_ownership(TOOLCHAIN_OWNERSHIP, database)
    print(f"Promoted: {owner_id}")
    print(f"Toolchain: {TOOLCHAIN}")
    print(f"Ownership: {TOOLCHAIN_OWNERSHIP}")


def command_sysroot(_args):
    database = _read_ownership(SYSROOT_OWNERSHIP, TARGET)
    print(f"Sysroot: {SYSROOT}")
    print(f"Target:  {TARGET}")
    packages = database["packages"]
    if not packages:
        print("Deployed packages: none")
        return
    print("Deployed packages:")
    for owner, entries in sorted(packages.items()):
        print(f"  {owner} ({len(entries)} owned files/links)")


def command_toolchain(_args):
    database = _read_ownership(TOOLCHAIN_OWNERSHIP, TARGET)
    print(f"Toolchain: {TOOLCHAIN}")
    print(f"Target:    {TARGET}")
    packages = database["packages"]
    if not packages:
        print("Promoted packages: none recorded")
        return
    print("Promoted packages:")
    for owner, entries in sorted(packages.items()):
        print(f"  {owner} ({len(entries)} owned files/links)")


def command_list(_args):
    recipes = sorted(RECIPES.glob("*.toml"))
    if not recipes:
        print("No package recipes found.")
        return
    for recipe_path in recipes:
        try:
            data = load_recipe(recipe_path.stem)
            dependencies = ", ".join(data.get("depends", [])) or "none"
            print(
                f"{data['name']} {data['version']} "
                f"[type: {data.get('build_type', 'host')}; dependencies: {dependencies}]"
            )
        except KBuildError as exc:
            print(f"{recipe_path.name}: INVALID ({exc})")


def command_info(args):
    data = load_recipe(args.name)
    print(f"Name:         {data['name']}")
    print(f"Version:      {data['version']}")
    print(f"Source:       {data['source']}")
    print(f"SHA-256:      {data['sha256']}")
    print(f"Build type:   {data.get('build_type', 'host')}")
    print(f"Dependencies: {', '.join(data.get('depends', [])) or 'none'}")
    print(f"Target:       {TARGET}")
    print(f"Build steps:  {len(data.get('build', []))}")
    print(f"Install steps:{len(data.get('install', []))}")


def command_fetch(args):
    data = load_recipe(args.name)
    print(f"Cached: {fetch(data).name}")


def command_verify(args):
    data = load_recipe(args.name)
    verify(fetch(data), data["sha256"])


def all_recipe_order():
    """Return every recipe once, with toolchain bootstrap recipes first."""
    recipe_names = sorted(
        path.stem for path in RECIPES.glob("*.toml") if path.is_file()
    )
    if not recipe_names:
        raise KBuildError("No package recipes found.")

    missing_bootstrap = [name for name in BOOTSTRAP_ORDER if name not in recipe_names]
    if missing_bootstrap:
        raise KBuildError(
            "Cannot bootstrap KhaymOS; missing required recipes: "
            + ", ".join(missing_bootstrap)
        )

    recipes = {}
    state = {}
    stack = []
    ordered = []

    def visit(name):
        status = state.get(name, 0)
        if status == 2:
            return
        if status == 1:
            cycle_start = stack.index(name)
            cycle = stack[cycle_start:] + [name]
            raise KBuildError(
                "Dependency cycle detected: " + " -> ".join(cycle)
            )

        data = recipes.get(name)
        if data is None:
            data = _read_recipe(name)
            recipes[name] = data

        state[name] = 1
        stack.append(name)
        for dependency in data.get("depends", []):
            visit(dependency)
        stack.pop()
        state[name] = 2
        ordered.append(name)

    # Seed the walk with the required bootstrap sequence. Remaining recipes are
    # topologically ordered afterward so they cannot run ahead of the toolchain.
    roots = list(BOOTSTRAP_ORDER)
    roots.extend(name for name in recipe_names if name not in set(roots))
    for name in roots:
        visit(name)

    return ordered


def command_build(args):
    if args.name != "all":
        if args.dry_run:
            raise KBuildError("--dry-run is only supported with 'build all'")
        build(args.name, force=args.force)
        return

    order = all_recipe_order()

    if args.dry_run:
        print(
            f"Build plan: {len(order)} recipes; bootstrap toolchain before "
            "building the remaining target system"
        )
        for index, name in enumerate(order, start=1):
            data = _read_recipe(name)
            dependencies = ", ".join(data.get("depends", [])) or "none"
            build_type = data.get("build_type", "host")
            action = BOOTSTRAP_ACTIONS.get(name)
            if action is None:
                action = (
                    "build + deploy to sysroot"
                    if build_type == "target"
                    else "build (host package)"
                )
            print(
                f"{index:2}/{len(order)}  {name} {data['version']} "
                f"[{build_type}; {action}; dependencies: {dependencies}]"
            )

        print()
        print("Bootstrap compiler order:")
        for index, name in enumerate(BOOTSTRAP_ORDER, start=1):
            print(f"  {index}. {name}")
        print()
        print("Dry run complete; no packages were built or deployed.")
        return

    bootstrap_names = set(BOOTSTRAP_ORDER)
    print("\n==> Bootstrapping KhaymOS cross-toolchain")
    for index, name in enumerate(BOOTSTRAP_ORDER, start=1):
        print(f"\n==> [bootstrap {index}/{len(BOOTSTRAP_ORDER)}] {name}")
        build(name, force=args.force)
        if name in ("binutils-cross", "gcc-cross-stage1", "gcc-cross"):
            promote(name)
        else:
            deploy(name)

    remaining = [name for name in order if name not in bootstrap_names]
    for index, name in enumerate(remaining, start=len(BOOTSTRAP_ORDER) + 1):
        data = load_recipe(name)
        print(f"\n==> [{index}/{len(order)}] Building {name}")
        build(name, force=args.force)
        if data.get("build_type", "host") == "target":
            deploy(name)

    print(f"\nKhaymOS build-all completed: {len(order)} recipes processed.")


def command_clean(args):
    clean(args.name, all_files=args.all)


def command_deploy(args):
    deploy(args.name)


def command_undeploy(args):
    undeploy(args.name)


def command_promote(args):
    promote(args.name)


def main():
    ensure_directories()
    parser = argparse.ArgumentParser(
        prog="kbuild.py",
        description=f"KBuild {VERSION} — KhaymOS Build System",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List package recipes")

    info_parser = subparsers.add_parser("info", help="Show recipe information")
    info_parser.add_argument("name")
    fetch_parser = subparsers.add_parser("fetch", help="Download package source")
    fetch_parser.add_argument("name")
    verify_parser = subparsers.add_parser("verify", help="Download and verify package source")
    verify_parser.add_argument("name")

    build_parser = subparsers.add_parser(
        "build", help="Build a package or bootstrap and build all recipes"
    )
    build_parser.add_argument("name")
    build_parser.add_argument(
        "--force", action="store_true",
        help="Rebuild this package even if its cache is valid",
    )
    build_parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview the staged bootstrap and dependency-first build-all plan",
    )

    clean_parser = subparsers.add_parser("clean", help="Remove build output")
    clean_parser.add_argument("name", nargs="?")
    clean_parser.add_argument(
        "--all", action="store_true",
        help="Remove build output and metadata, preserving downloads, sysroot, and toolchain",
    )

    deploy_parser = subparsers.add_parser("deploy", help="Deploy a target package into sysroot")
    deploy_parser.add_argument("name")
    undeploy_parser = subparsers.add_parser("undeploy", help="Remove a package from sysroot")
    undeploy_parser.add_argument("name")
    promote_parser = subparsers.add_parser("promote", help="Promote host-built cross-tools into toolchain")
    promote_parser.add_argument("name")
    subparsers.add_parser("sysroot", help="Show sysroot and deployed-package ownership")
    subparsers.add_parser("toolchain", help="Show promoted toolchain-package ownership")

    args = parser.parse_args()
    actions = {
        "list": command_list,
        "info": command_info,
        "fetch": command_fetch,
        "verify": command_verify,
        "build": command_build,
        "clean": command_clean,
        "deploy": command_deploy,
        "undeploy": command_undeploy,
        "promote": command_promote,
        "sysroot": command_sysroot,
        "toolchain": command_toolchain,
    }
    try:
        actions[args.command](args)
    except (KBuildError, OSError) as exc:
        print(f"KBuild error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
