# SquashFS live ISO (experimental)

This path builds a separate live ISO that stores the assembled system in a
compressed SquashFS file on the ISO. A compact initramfs finds the ISO9660
medium, mounts the SquashFS image, provides a writable OverlayFS upper layer,
and switches to OpenRC. The original cpio-rootfs ISO builder and existing
images remain unchanged.

## Requirements

The Artix build host needs squashfs-tools (mksquashfs and unsquashfs),
GRUB ISO tooling (grub-mkrescue and xorriso), cpio, and the regular build
tools already used by KBuild.

The kernel recipe makes the early-boot storage and filesystem stack built
in, including USB mass storage, ISO9660, loop devices, SquashFS/XZ and
OverlayFS. Those drivers cannot be modules in the SquashFS image because the
image must be mounted before that image's /lib/modules is available.

## Build an isolated diagnostic image

Use a new tag and rootfs so earlier root filesystems, ISOs, and installed-test
disks are preserved. Start in the KBuild checkout:

    cd ~/Projects/KhaymOS/kbuild/gh/KBuild

    TAG=20261028-live-squashfs-v1
    ROOTFS="$PWD/rootfs-iso-$TAG"
    PACKAGES_DIR="$PWD/packages-iso-20261025-net-v1"

    # Rebuild and deploy the kernel after changing recipes/linux-kernel.toml.
    python3 kbuild.py build linux-kernel
    python3 kbuild.py deploy linux-kernel

    # Assemble a fresh rootfs from the updated sysroot.
    if [ -e "$ROOTFS" ]; then
        echo "Refusing to replace existing rootfs: $ROOTFS"
    else
        sudo env ROOTFS="$ROOTFS" bash scripts/assemble-rootfs.sh
    fi

    # Set the live image's root password before making the image.
    sudo chroot "$ROOTFS" /usr/bin/passwd root

    # Ensure squashfs-tools is installed, then build a new tagged ISO.
    sudo env TAG="$TAG" ROOTFS="$ROOTFS" \
        KBUILD_PACKAGES_DIR="$PACKAGES_DIR" \
        bash scripts/build-live-squashfs-iso.sh

The builder refuses to overwrite an existing tag-specific initramfs, ISO tree,
or ISO file. If the tag already exists, choose a new tag rather than deleting
its outputs.

## First tests

Boot the ISO in QEMU first with 4 GiB and then 2 GiB of RAM. The normal menu
entry uses the visible VGA console and verbose kernel messages; the second
entry provides a serial console for -nographic diagnostics. Verify that
fastfetch reports the live system and that / is OverlayFS-backed while
/run/live-lower is SquashFS.

For physical USB tests, write the ISO as a raw/hybrid image with a suitable
imaging tool. Copying the ISO file onto a separately formatted FAT partition
is not supported by the early-media search in this experimental version.

The installed-system installer remains the existing BIOS/MBR-only proof of
concept. Successful live boot does not itself validate installation, UEFI
installation, Wi-Fi hardware, or support for every USB storage controller.
