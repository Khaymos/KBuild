import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make-repo-index.py"
SPEC = importlib.util.spec_from_file_location("make_repo_index", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
INDEXER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INDEXER)


def write_package(
    path: Path, name: str, version: str = "1.0", extra_metadata_line: str = ""
) -> None:
    metadata = (
        "format=KHMPKG-1\n"
        f"name={name}\n"
        f"version={version}\n"
        "arch=x86_64\n"
        f"description={name} test package\n"
        "depends=\n"
        f"{extra_metadata_line}"
    ).encode()
    manifest = b"F\t0755\t" + b"0" * 64 + b"\tusr/bin/example\n"

    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for member_name, data in (
            ("CONTROL/metadata", metadata),
            ("CONTROL/manifest", manifest),
        ):
            info = tarfile.TarInfo(member_name)
            info.mode = 0o644
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


class RepositoryIndexTests(unittest.TestCase):
    def test_builds_sorted_index_with_full_file_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packages = root / "packages"
            packages.mkdir()
            write_package(packages / "zeta-1.0.khmpkg", "zeta")
            write_package(packages / "alpha-1.0.khmpkg", "alpha")
            output = root / "index.tsv"

            count = INDEXER.write_index(packages, output)

            self.assertEqual(count, 2)
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], INDEXER.INDEX_MAGIC)
            self.assertEqual(lines[1], "\t".join(INDEXER.INDEX_FIELDS))
            self.assertEqual(lines[2].split("\t")[0], "alpha")
            self.assertEqual(lines[3].split("\t")[0], "zeta")

            record = lines[2].split("\t")
            self.assertEqual(record[4], hashlib.sha256(
                (packages / record[3]).read_bytes()
            ).hexdigest())

    def test_rejects_duplicate_package_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packages = root / "packages"
            packages.mkdir()
            write_package(packages / "demo-1.0-a.khmpkg", "demo")
            write_package(packages / "demo-1.0-b.khmpkg", "demo")

            with self.assertRaisesRegex(ValueError, "duplicate package identity"):
                INDEXER.collect_records(packages)

    def test_rejects_unknown_metadata_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "demo-1.0.khmpkg"
            write_package(
                archive_path,
                "demo",
                extra_metadata_line="unexpected=value\n",
            )

            with self.assertRaisesRegex(ValueError, "unknown metadata key"):
                INDEXER.package_record(archive_path)

    def test_rejects_empty_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packages = Path(tmp) / "packages"
            packages.mkdir()
            with self.assertRaisesRegex(ValueError, "no .khmpkg files"):
                INDEXER.collect_records(packages)


    def test_rejects_symlinked_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packages = root / "packages"
            packages.mkdir()
            alias = root / "packages-link"
            alias.symlink_to(packages, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                INDEXER.write_index(alias, root / "index.tsv")


if __name__ == "__main__":
    unittest.main()
