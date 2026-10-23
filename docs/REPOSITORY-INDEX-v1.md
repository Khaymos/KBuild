# KhaymOS Repository Index v1

The repository base URL from a KNPM mirrorlist points to a directory containing `index.tsv` and the `packages/` directory.

For the official x86-64 mirror, the planned layout is:

```text
/repo/
  mirrorlists/
    official.list
  x86_64/
    khaymos/
      index.tsv
      packages/
        example-1.0.khmpkg
```

## Index format

The index is UTF-8 text with LF line endings and seven tab-separated fields per package record. The first line is the required magic value; the second line names the fields in order.

```text
# KHAYMOS-REPO-INDEX-1
name	version	arch	filename	sha256	depends	description
```

The fields are:

1. `name`: package name from `CONTROL/metadata`.
2. `version`: package version.
3. `arch`: `x86_64` or `noarch`.
4. `filename`: basename of the downloadable archive, ending in `.khmpkg`.
5. `sha256`: lowercase SHA-256 of the entire compressed package file.
6. `depends`: comma-separated dependency names, or an empty field.
7. `description`: single-line package description.

The archive is fetched relative to the mirror base URL at `packages/FILENAME`. Package records are sorted by name, architecture, version, and filename. Duplicate `name/version/arch` identities are rejected.

## Generate an index

From the KBuild checkout, point the script at the directory containing package archives and write the index beside the `packages/` directory:

```sh
python3 scripts/make-repo-index.py \
  --packages-dir packages \
  --output /tmp/khaymos-repo/index.tsv
```

Upload `index.tsv` to the repository base directory after the referenced packages are present on the server. Upload package files first and publish the index last, so clients do not see records for files that have not finished uploading.

## Integrity and trust

The index SHA-256 lets KNPM reject a downloaded archive that does not match the indexed bytes. The package's own `CONTROL/manifest` is still verified by KNPM before installation. SHA-256 does not authenticate an index by itself; public mirrors should use HTTPS. A signed index can be added in a later version.
