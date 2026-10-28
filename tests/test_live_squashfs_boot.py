from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LiveSquashFSBootTests(unittest.TestCase):
    def test_live_boot_scripts_have_valid_bash_syntax(self) -> None:
        for name in (
            "build-live-initramfs.sh",
            "build-live-squashfs-iso.sh",
        ):
            with self.subTest(script=name):
                result = subprocess.run(
                    ["bash", "-n", str(ROOT / "scripts" / name)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_kernel_recipe_builds_live_storage_and_filesystems_in(self) -> None:
        recipe = (ROOT / "recipes" / "linux-kernel.toml").read_text(
            encoding="utf-8"
        )
        for option in (
            "BLK_DEV_LOOP",
            "OVERLAY_FS",
            "SQUASHFS",
            "SQUASHFS_XZ",
            "ISO9660_FS",
            "BLK_DEV_SR",
            "USB_STORAGE",
            "USB_PCI",
            "USB_XHCI_HCD",
            "USB_XHCI_PCI",
            "USB_EHCI_HCD",
            "USB_EHCI_PCI",
            "USB_HID",
        ):
            with self.subTest(option=option):
                self.assertIn(f"--enable {option}", recipe)
                self.assertIn(f" {option} ", recipe)
        self.assertIn('CONFIG_${option}=y', recipe)

    def test_live_iso_uses_a_small_initramfs_and_squashfs_root(self) -> None:
        builder = (ROOT / "scripts" / "build-live-squashfs-iso.sh").read_text(
            encoding="utf-8"
        )
        initramfs = (ROOT / "scripts" / "build-live-initramfs.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("mksquashfs", builder)
        self.assertIn("/live/khaymos-rootfs.squashfs", initramfs)
        self.assertIn("mount -t squashfs -o loop,ro", initramfs)
        self.assertIn("mount -t overlay overlay", initramfs)
        self.assertIn("mount --move /run/live-media", initramfs)
        self.assertIn("console=tty0 rdinit=/init", builder)
        self.assertIn("loglevel=7 ignore_loglevel", builder)
        first_entry = builder.split('menuentry "KhaymOS GNU/Linux 0.1 (Live, serial console)"')[0]
        self.assertNotIn("terminal_output serial", first_entry)

    def test_assembled_system_has_a_visible_vt1_login(self) -> None:
        assembly = (ROOT / "scripts" / "assemble-rootfs.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('ln -sfn agetty "$ROOTFS/etc/init.d/agetty.tty1"', assembly)
        self.assertIn('"$ROOTFS/etc/runlevels/default/agetty.tty1"', assembly)


if __name__ == "__main__":
    unittest.main()
