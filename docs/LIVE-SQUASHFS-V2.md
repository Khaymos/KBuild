# SquashFS live ISO v2

This experimental live image keeps the operating-system tree in a compressed
SquashFS file on the ISO and uses a small initramfs only to locate the ISO,
mount SquashFS, create a RAM-backed writable overlay, and switch into OpenRC.
The existing `scripts/build-iso.sh` and existing installer ISOs remain unchanged.

## Important design requirements

- The kernel must have ISO9660, optical-media, loop, SquashFS/XZ, overlayfs,
  USB mass-storage, common USB host controllers, and USB HID/input support built
  in (`=y`). Modules inside SquashFS cannot be loaded before the live root is
  mounted.
- The live root is read-only SquashFS with a tmpfs upper/work overlay. Runtime
  changes are lost at reboot.
- The initramfs searches optical, USB/SCSI, NVMe, and eMMC block-device nodes.
  If it cannot find the ISO or mount the image, it drops to a minimal Bash shell
  and prints a visible error.
- GRUB uses the normal display and also asks Linux to log to the serial console;
  kernel messages are deliberately verbose for hardware diagnosis.
- This is a diagnostic build. Do not install it on physical disks.

## Build sequence

1. Update the checkout to this branch and install host `squashfs-tools`.
2. Rebuild and deploy `linux-kernel` using the updated
   `recipes/linux-kernel.toml`. KBuild's normal target recipe build/deploy
   flow should be used so both the kernel image and `config-7.2.10` in the
   sysroot are refreshed.
3. Assemble a fresh rootfs in a new, unique directory using
   `scripts/assemble-rootfs.sh`; set the root password with chroot as usual.
4. Build into new, unique paths, never reusing an earlier ISO or rootfs:

```bash
TAG=20261028-live-squashfs-v1
ROOTFS="$PWD/rootfs-iso-$TAG"
ISO_ROOT="$PWD/build/khaymos-live-$TAG"
DIST="$PWD/dist-$TAG"
ISO="$DIST/khaymos-0.1-x86_64-$TAG.iso"
INITRD="$PWD/build/khaymos-live-initramfs-$TAG.cpio.gz"
SQUASHFS="$ISO_ROOT/live/rootfs.squashfs"

sudo env \
  ROOTFS="$ROOTFS" \
  KERNEL="$ROOTFS/boot/vmlinuz-khaymos-7.2.10" \
  INITRD="$INITRD" \
  INITRD_TMP="$INITRD.new" \
  ISO_ROOT="$ISO_ROOT" \
  DIST="$DIST" \
  ISO="$ISO" \
  SQUASHFS="$SQUASHFS" \
  bash scripts/build-live-iso-squashfs.sh
```

The rootfs must already exist before this command. Set `KBUILD_PACKAGES_DIR`
if the package archives are in a non-default directory.

## Validation

- Boot the ISO in QEMU at 2 GiB first; verify the live system reaches OpenRC.
- Check `free -h`, `findmnt /`, and `mount | grep -E 'squashfs|overlay|iso9660'`.
- Confirm root is an overlay over SquashFS, and that writes work in the upper layer.
- Then test 4 GiB QEMU and only afterward try Khayil's hardware.
- The installer remains BIOS/MBR-only and destructive. Do not test it on real
  disks as part of live-boot diagnostics.
