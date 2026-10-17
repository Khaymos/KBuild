
#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

TOP="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

ROOTFS="$TOP/rootfs"
KERNEL="$ROOTFS/boot/vmlinuz-khaymos-7.2.10"

INITRD="/tmp/khaymos-rootfs.cpio.gz"
INITRD_TMP="$TOP/build/khaymos-rootfs.cpio.gz.new"

ISO_ROOT="$TOP/build/khaymos-iso"
DIST="$TOP/dist"
ISO="$DIST/khaymos-0.1-dev-x86_64.iso"

for tool in grub-mkrescue xorriso gzip cpio find python3; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: required host tool missing: $tool" >&2
        exit 1
    }
done

if [[ ! -d "$ROOTFS" ]]; then
    echo "ERROR: rootfs directory is missing: $ROOTFS" >&2
    exit 1
fi

if [[ ! -r "$KERNEL" ]]; then
    echo "ERROR: KhaymOS kernel missing: $KERNEL" >&2
    exit 1
fi

echo "=== Bootstrapping KNPM package database ==="
python3 "$TOP/scripts/bootstrap-knpm.py" "$ROOTFS"

if [[ ! -x "$ROOTFS/usr/bin/knpm" ]]; then
    echo "ERROR: KNPM is missing from rootfs/usr/bin/knpm." >&2
    exit 1
fi

mkdir -p "$TOP/build" "$DIST" "$ISO_ROOT/boot/grub"

echo "=== Rebuilding initramfs from current rootfs ==="

rm -f "$INITRD_TMP"

(
    cd "$ROOTFS"
    find . -print0 |
        cpio --null -o --format=newc |
        gzip -9 > "$INITRD_TMP"
)

echo
echo "=== Validating initramfs ==="

gzip -t "$INITRD_TMP"

if ! gzip -dc "$INITRD_TMP" | cpio -it --quiet |
    grep -E '(^|/)usr/bin/knpm$' >/dev/null; then
    echo "ERROR: KNPM was not found in the generated initramfs." >&2
    exit 1
fi

mv -f "$INITRD_TMP" "$INITRD"

ls -lh "$INITRD"

echo
echo "=== Validating kernel ==="

file "$KERNEL"

echo
echo "=== Preparing GRUB ISO tree ==="

install -m 0644 "$KERNEL" \
    "$ISO_ROOT/boot/vmlinuz-khaymos-7.2.10"

install -m 0644 "$INITRD" \
    "$ISO_ROOT/boot/khaymos-rootfs.cpio.gz"

cat > "$ISO_ROOT/boot/grub/grub.cfg" <<'GRUB'
set default=0
set timeout=5

insmod serial
serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1
terminal_input serial
terminal_output serial

menuentry "KhaymOS GNU/Linux 0.1-dev (Live)" {
    echo 'Loading Linux 7.2.10 ...'
    linux /boot/vmlinuz-khaymos-7.2.10 console=ttyS0,115200 rdinit=/init hostname=khaymos quiet loglevel=3 panic=10
    echo 'Loading initial ramdisk ...'
    initrd /boot/khaymos-rootfs.cpio.gz
}
GRUB

echo
echo "=== Building KhaymOS ISO ==="

rm -f "$ISO"
grub-mkrescue -o "$ISO" "$ISO_ROOT"

echo
echo "=== Validating ISO ==="

test -s "$ISO"
file "$ISO"
ls -lh "$ISO"

echo
echo "ISO created: $ISO"
