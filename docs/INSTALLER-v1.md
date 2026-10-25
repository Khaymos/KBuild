# KhaymOS Installer v1

The first installer is an interactive, BIOS-only proof of concept. It installs
the live KhaymOS filesystem onto one ext4 root partition and puts GRUB's
`i386-pc` bootloader in the disk's MBR.

## Run it

Boot a KhaymOS live ISO as root and run:

```sh
khaymos-installer
```

The installer lists detected disks. Enter the full path of the intended target
disk, review its model and size, and type the exact confirmation it requests.
It refuses partitions, optical drives, mounted disks, disks smaller than 8 GiB,
and non-interactive execution. **The selected disk's existing partition table
and all data on it will be destroyed.**

The installer creates one bootable MBR partition, formats it ext4, copies the
live system, asks for a hostname and installed root password, generates a
new D-Bus machine ID, installs GRUB for legacy BIOS, and writes a UUID-based
GRUB entry. Kernel storage drivers and ext4 are built in, so v1 boots the
installed root filesystem directly without an initramfs.

## Limitations

- Legacy BIOS/MBR only; UEFI/GPT support is not implemented.
- One ext4 root partition; no separate boot, home, or swap partition.
- Does not configure encryption, automatic partition resizing, or dual boot.
- The first QEMU test must use a disposable virtual disk. Do not test on a
  personal or system disk.
- The installed OS still needs a boot test; successful installer execution
  alone does not prove the installed system will boot.

The original live ISO is not modified by installation.
