# KHMPKG Format v1

KhaymOS package archive format, version 1.

## Container

A .khmpkg archive is a gzip-compressed PAX tar archive.

Required members:

- CONTROL/metadata
- CONTROL/manifest
- data/

The data directory mirrors paths relative to the target filesystem.
For example, data/usr/bin/example installs as /usr/bin/example.

## Metadata

CONTROL/metadata uses UTF-8 key=value records:

format=KHMPKG-1
name=example
version=1.0
arch=x86_64
description=Example package
depends=

Names and versions must not contain whitespace or line breaks.
The depends field is a comma-separated list of package names.

## Manifest

CONTROL/manifest is UTF-8, tab-separated, one entry per line:

TYPE<TAB>MODE<TAB>SHA256<TAB>RELATIVE_PATH

TYPE is F for a regular file, D for a directory, or L for a
symbolic link. MODE is the four-digit octal permission mode.
SHA256 is the content hash for regular files, the hash of the
symbolic-link target for links, and "-" for directories.

Paths must be relative and must not contain "." or ".." components,
tabs, carriage returns, or line feeds. Special files are not supported.

## v1 scope

Local packages only. No package scripts, remote repositories,
signatures, automatic dependency resolution, upgrades, or
dependency removal are defined in this version.

SHA-256 detects payload corruption, but an unsigned package is
not authenticated. Only install packages from trusted sources.
