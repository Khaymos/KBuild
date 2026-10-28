#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: run as root, preserving the same TAG and path environment, for example:" >&2
    echo "  sudo env TAG=20261028-live-squashfs-v1 ROOTFS=\"$PWD/rootfs-iso-20261028-live-squashfs-v1\" bash scripts/build-live-squashfs-iso.sh" >&2
    exit 1
fi

TOP="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${TAG:-}"
[[ -n "$TAG" ]] || {
    echo "ERROR: TAG is required so this builder cannot overwrite an existing ISO by default." >&2
    exit 1
}
[[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "ERROR: TAG contains unsupported characters: $TAG" >&2
    exit 1
}

ROOTFS="${ROOTFS:-$TOP/rootfs-iso-$TAG}"
KERNEL="${KERNEL:-$ROOTFS/boot/vmlinuz-khaymos-7.2.10}"
INITRD="${INITRD:-$TOP/build/khaymos-live-initramfs-$TAG.cpio.gz}"
ISO_ROOT="${ISO_ROOT:-$TOP/build/khaymos-iso-$TAG}"
DIST="${DIST:-$TOP/dist-$TAG}"
ISO="${ISO:-$DIST/khaymos-0.1-x86_64-$TAG.iso}"
SQUASHFS="$ISO_ROOT/live/khaymos-rootfs.squashfs"

for tool in grub-mkrescue xorriso cpio gzip find grep python3 mksquashfs unsquashfs readelf realpath install file ls sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: required host tool missing: $tool" >&2
        if [[ "$tool" == mksquashfs || "$tool" == unsquashfs ]]; then
            echo "Install squashfs-tools on the Artix build host." >&2
        fi
        exit 1
    }
done

[[ -d "$ROOTFS" ]] || {
    echo "ERROR: rootfs directory is missing: $ROOTFS" >&2
    exit 1
}
[[ -r "$KERNEL" ]] || {
    echo "ERROR: KhaymOS kernel missing: $KERNEL" >&2
    exit 1
}
for path in "$INITRD" "$ISO_ROOT" "$ISO"; do
    [[ ! -e "$path" ]] || {
        echo "ERROR: refusing to overwrite existing build output: $path" >&2
        exit 1
    }
done

mkdir -p "$TOP/build" "$DIST" "$ISO_ROOT/boot/grub" "$ISO_ROOT/live"

# This updates the local KNPM database in this newly assembled rootfs, just as
# the original ISO builder does. The existing live/installer images are intact.
echo "=== Bootstrapping KNPM package database ==="
python3 "$TOP/scripts/bootstrap-knpm.py" "$ROOTFS"
[[ -x "$ROOTFS/usr/bin/knpm" ]] || {
    echo "ERROR: KNPM is missing from $ROOTFS/usr/bin/knpm." >&2
    exit 1
}

# Refuse to proceed unless the kernel has the essential live-root features built
# in. Modules cannot help mount the USB medium before the SquashFS root exists.
CONFIG="$ROOTFS/boot/config-7.2.10"
[[ -r "$CONFIG" ]] || {
    echo "ERROR: kernel config missing: $CONFIG" >&2
    echo "Rebuild and deploy linux-kernel with the SquashFS live-boot options first." >&2
    exit 1
}
for option in BLK_DEV_LOOP OVERLAY_FS SQUASHFS SQUASHFS_XZ ISO9660_FS SCSI BLK_DEV_SD BLK_DEV_SR USB USB_PCI USB_STORAGE USB_XHCI_HCD USB_XHCI_PCI; do
    grep -qx "CONFIG_${option}=y" "$CONFIG" || {
        echo "ERROR: required live-boot kernel option is not built in: CONFIG_${option}=y" >&2
        echo "Rebuild and deploy the kernel recipe from the live-squashfs branch first." >&2
        exit 1
    }
done

# The image and initramfs are new, tag-specific outputs. SquashFS remains on the
# ISO, so GRUB only loads the small early-userspace archive into memory.
echo
echo "=== Building compressed SquashFS root ==="
mksquashfs "$ROOTFS" "$SQUASHFS" -noappend -comp xz -b 1M -processors 1
unsquashfs -s "$SQUASHFS"
if ! unsquashfs -ll "$SQUASHFS" | grep -E 'squashfs-root/usr/bin/knpm$' >/dev/null; then
    echo "ERROR: KNPM was not found in the SquashFS root image." >&2
    exit 1
fi

printf '\n=== Building compact live initramfs ===\n'
ROOTFS="$ROOTFS" INITRD="$INITRD" bash "$TOP/scripts/build-live-initramfs.sh"

echo
echo "=== Preparing GRUB ISO tree ==="
install -m 0644 "$KERNEL" "$ISO_ROOT/boot/vmlinuz-khaymos-7.2.10"
install -m 0644 "$INITRD" "$ISO_ROOT/boot/khaymos-initramfs.cpio.gz"

cat > "$ISO_ROOT/boot/grub/grub.cfg" <<'GRUB'
set default=0
set timeout=5

insmod all_video
insmod gfxterm
insmod iso9660
insmod part_msdos
insmod part_gpt

menuentry "KhaymOS GNU/Linux 0.1 (Live, SquashFS)" {
    echo 'Loading KhaymOS Linux 7.2.10 ...'
    linux /boot/vmlinuz-khaymos-7.2.10 console=tty0 rdinit=/init hostname=khaymos loglevel=7 ignore_loglevel panic=10
    echo 'Loading compact live initramfs ...'
    initrd /boot/khaymos-initramfs.cpio.gz
}

menuentry "KhaymOS GNU/Linux 0.1 (Live, serial console)" {
    insmod serial
    serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1
    terminal_input serial
    terminal_output serial
    echo 'Loading KhaymOS Linux 7.2.10 ...'
    linux /boot/vmlinuz-khaymos-7.2.10 console=ttyS0,115200 rdinit=/init hostname=khaymos loglevel=7 ignore_loglevel panic=10
    echo 'Loading compact live initramfs ...'
    initrd /boot/khaymos-initramfs.cpio.gz
}
GRUB

echo
echo "=== Building diagnostic live ISO ==="
grub-mkrescue -o "$ISO" "$ISO_ROOT"

echo
echo "=== Validating ISO ==="
test -s "$ISO"
file "$ISO"
ls -lh "$INITRD" "$SQUASHFS" "$ISO"
sha256sum "$ISO"
echo
echo "ISO created: $ISO"
echo "The previous cpio-based ISO and installed-system test disks were not modified."
