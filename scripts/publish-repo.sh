#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

TOP="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGES_DIR="${1:-$TOP/packages}"
HOST="${KHAYMOS_REPO_HOST:-khaymos-repo}"

REMOTE_ROOT='/srv/khaymos/repo/x86_64/khaymos'
REMOTE_PACKAGES="$REMOTE_ROOT/packages"
REMOTE_LIST='/srv/khaymos/repo/mirrorlists/official.list'
MIRRORLIST="$TOP/config/official.mirrorlist"

if [[ $# -gt 1 ]]; then
    echo "Usage: $0 [PACKAGES_DIR]" >&2
    exit 2
fi

if [[ ! -d "$PACKAGES_DIR" || -L "$PACKAGES_DIR" ]]; then
    echo "ERROR: package directory is missing or unsafe: $PACKAGES_DIR" >&2
    exit 1
fi

if [[ ! -f "$MIRRORLIST" || -L "$MIRRORLIST" ]]; then
    echo "ERROR: official mirrorlist is missing: $MIRRORLIST" >&2
    exit 1
fi

for tool in python3 ssh scp; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: required host tool missing: $tool" >&2
        exit 1
    }
done

shopt -s nullglob
packages=("$PACKAGES_DIR"/*.khmpkg)
if [[ ${#packages[@]} -eq 0 ]]; then
    echo "ERROR: no .khmpkg files found in $PACKAGES_DIR" >&2
    exit 1
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/khaymos-repo-publish.XXXXXX")"
trap 'rm -rf -- "$TMP"' EXIT
INDEX_FILE="$TMP/index.tsv"

# Validate all archives and build the index locally before uploading anything.
python3 "$TOP/scripts/make-repo-index.py" \
    --packages-dir "$PACKAGES_DIR" \
    --output "$INDEX_FILE"

echo "=== Checking repository server and preparing directories ==="
ssh "$HOST" \
    'mkdir -p /srv/khaymos/repo/x86_64/khaymos/packages /srv/khaymos/repo/mirrorlists &&
     chmod 0755 /srv/khaymos/repo/x86_64/khaymos /srv/khaymos/repo/x86_64/khaymos/packages /srv/khaymos/repo/mirrorlists'

echo
echo "=== Uploading package archives ==="
for package in "${packages[@]}"; do
    filename="${package##*/}"
    printf 'Uploading %s\n' "$filename"
    scp -- "$package" "$HOST:$REMOTE_PACKAGES/$filename"
done

ssh "$HOST" \
    'find /srv/khaymos/repo/x86_64/khaymos/packages -maxdepth 1 -type f -name "*.khmpkg" -exec chmod 0644 {} +'

echo
echo "=== Publishing official mirrorlist ==="
scp -- "$MIRRORLIST" "$HOST:$REMOTE_LIST"
ssh "$HOST" 'chmod 0644 /srv/khaymos/repo/mirrorlists/official.list'

echo
echo "=== Publishing repository index last ==="
scp -- "$INDEX_FILE" "$HOST:$REMOTE_ROOT/index.tsv"
ssh "$HOST" 'chmod 0644 /srv/khaymos/repo/x86_64/khaymos/index.tsv'

echo
printf 'Published %s local package archive(s) to %s.\n' "${#packages[@]}" "$HOST"
echo "Remote index: $REMOTE_ROOT/index.tsv"
echo "No remote packages were deleted."
