#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

TOP="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROOTFS="${ROOTFS:-$TOP/rootfs}"
KERNEL="${KERNEL:-$ROOTFS/boot/vmlinuz-khaymos-7.2.10}"
INITRD="${INITRD:-$TOP/build/khaymos-live-initramfs.cpio.gz}"
INITRD_TMP="${INITRD_TMP:-$INITRD.new}"
ISO_ROOT="${ISO_ROOT:-$TOP/build/khaymos-live-squashfs-iso}"
DIST="${DIST:-$TOP/dist-live-squashfs}"
ISO="${ISO:-$DIST/khaymos-0.1-x86_64-live-squashfs.iso}"
SQUASHFS="${SQUASHFS:-$ISO_ROOT/live/rootfs.squashfs}"

for tool in grub-mkrescue xorriso mksquashfs gzip cpio python3 file; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: required host tool missing: $tool" >&2
        exit 1
    }
done

[[ -d "$ROOTFS" ]] || { echo "ERROR: rootfs directory missing: $ROOTFS" >&2; exit 1; }
[[ -r "$KERNEL" ]] || { echo "ERROR: KhaymOS kernel missing: $KERNEL" >&2; exit 1; }
[[ -x "$ROOTFS/usr/bin/knpm" ]] || { echo "ERROR: KNPM missing from $ROOTFS" >&2; exit 1; }

echo "=== Bootstrapping KNPM package database ==="
python3 "$TOP/scripts/bootstrap-knpm.py" "$ROOTFS"

mkdir -p "$TOP/build" "$DIST" "$ISO_ROOT/boot/grub" "$ISO_ROOT/live"
rm -f "$SQUASHFS" "$INITRD_TMP"

echo
echo "=== Building compressed SquashFS live root ==="
mksquashfs "$ROOTFS" "$SQUASHFS" -noappend -comp xz -b 1M -all-root -no-progress
test -s "$SQUASHFS"
file "$SQUASHFS"
ls -lh "$SQUASHFS"

echo
echo "=== Building minimal live initramfs ==="
python3 "$TOP/scripts/make-live-initramfs.py" --rootfs "$ROOTFS" --output "$INITRD_TMP"
gzip -t "$INITRD_TMP"
if ! gzip -dc "$INITRD_TMP" | cpio -it --quiet | grep -qx 'init'; then
    echo "ERROR: minimal initramfs does not contain /init" >&2
    exit 1
fi
mv -f "$INITRD_TMP" "$INITRD"
ls -lh "$INITRD"

echo
echo "=== Validating kernel live-media support ==="
CONFIG="$ROOTFS/boot/config-7.2.10"
if [[ -r "$CONFIG" ]]; then
    for option in BLK_DEV_SR ISO9660_FS SQUASHFS SQUASHFS_XZ OVERLAY_FS BLK_DEV_LOOP USB_STORAGE USB_UAS USB_XHCI_HCD USB_XHCI_PCI USB_EHCI_HCD USB_EHCI_PCI USB_OHCI_HCD USB_OHCI_HCD_PCI USB_UHCI_HCD USB_HID HID_GENERIC INPUT_EVDEV; do
        if ! grep -qx "CONFIG_${option}=y" "$CONFIG"; then
            echo "ERROR: kernel config lacks built-in CONFIG_${option}=y" >&2
            echo "Rebuild linux-kernel-target after updating recipes/linux-kernel.toml." >&2
            exit 1
        fi
    done
else
    echo "WARNING: $CONFIG is missing; cannot verify built-in live-media drivers." >&2
fi

echo
echo "=== Preparing GRUB ISO tree ==="
install -m 0644 "$KERNEL" "$ISO_ROOT/boot/vmlinuz-khaymos-7.2.10"
install -m 0644 "$INITRD" "$ISO_ROOT/boot/khaymos-live-initramfs.cpio.gz"

cat > "$ISO_ROOT/boot/grub/grub.cfg" <<'GRUB'
set default=0
set timeout=8

insmod all_video

menuentry "KhaymOS GNU/Linux 0.1 (Live, SquashFS)" {
    echo 'Loading KhaymOS Linux 7.2.10 ...'
    linux /boot/vmlinuz-khaymos-7.2.10 console=ttyS0,115200 console=tty0 rdinit=/init hostname=khaymos loglevel=7 ignore_loglevel panic=10
    echo 'Loading minimal live initramfs ...'
    initrd /boot/khaymos-live-initramfs.cpio.gz
}
GRUB

echo
echo "=== Building diagnostic live ISO ==="
rm -f "$ISO"
grub-mkrescue -o "$ISO" "$ISO_ROOT"

echo
echo "=== Validating ISO ==="
test -s "$ISO"
file "$ISO"
ls -lh "$ISO"
echo
echo "ISO created: $ISO"
