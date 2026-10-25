from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IsoPackageListTests(unittest.TestCase):
    def test_iso_builder_and_bootstrap_use_the_same_package_set_and_order(self) -> None:
        builder = load_script("build_iso_packages", "build-iso-packages.py")
        bootstrap = load_script("bootstrap_knpm", "bootstrap-knpm.py")

        expected = [
            (name, version, f"{name}-{version}.khmpkg")
            for name, version, _depends in builder.PACKAGES_TO_BUILD
        ]
        self.assertEqual(bootstrap.BOOTSTRAP_PACKAGES, expected)

    def test_runtime_dependencies_are_available_before_each_package(self) -> None:
        builder = load_script("build_iso_packages", "build-iso-packages.py")
        seen: set[str] = set()

        for name, _version, depends in builder.PACKAGES_TO_BUILD:
            with self.subTest(package=name):
                for dependency in filter(None, depends.split(",")):
                    self.assertIn(
                        dependency,
                        seen,
                        f"{name} runtime dependency {dependency} must be earlier in the list",
                    )
                seen.add(name)

    def test_knpm_is_bootstrapped_after_its_remote_runtime_dependencies(self) -> None:
        builder = load_script("build_iso_packages", "build-iso-packages.py")
        names = [name for name, _version, _depends in builder.PACKAGES_TO_BUILD]
        self.assertLess(names.index("curl-target"), names.index("knpm-target"))
        self.assertLess(
            names.index("ca-certificates-target"),
            names.index("knpm-target"),
        )
        self.assertLess(names.index("openssl-target"), names.index("curl-target"))


    def test_openrc_archive_excludes_runlevel_links_pruned_by_rootfs_assembly(self) -> None:
        builder = load_script("build_iso_packages", "build-iso-packages.py")
        assembly = (ROOT / "scripts" / "assemble-rootfs.sh").read_text(
            encoding="utf-8"
        )

        expected = {
            f"etc/runlevels/{level}/{service}"
            for level in ("boot", "default")
            for service in ("hostname", "keymaps", "save-keymaps", "netmount")
        }
        self.assertEqual(set(builder.PACKAGE_EXCLUDES["openrc-target"]), expected)

        # The service definition remains package-owned; only its runlevel links
        # are omitted from the bootstrap archive and disabled in the rootfs.
        self.assertNotIn(
            'rm -f "$ROOTFS/etc/init.d/netmount"',
            assembly,
        )
        self.assertIn(
            'rm -f "$ROOTFS/etc/runlevels/$level/$service"',
            assembly,
        )


    def test_iso_branding_uses_the_public_0_1_version(self) -> None:
        builder_script = (ROOT / "scripts" / "build-iso.sh").read_text(
            encoding="utf-8"
        )
        rootfs_script = (ROOT / "scripts" / "assemble-rootfs.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("khaymos-0.1-x86_64.iso", builder_script)
        self.assertIn('KhaymOS GNU/Linux 0.1 (Live)', builder_script)
        self.assertIn('PRETTY_NAME="KhaymOS Linux 0.1"', rootfs_script)
        self.assertIn('VERSION="0.1"', rootfs_script)
        self.assertNotIn("0.1-dev", builder_script)
        self.assertNotIn("0.1-dev", rootfs_script)


    def test_live_iso_contains_wired_and_wireless_networking(self) -> None:
        builder = load_script("build_iso_packages", "build-iso-packages.py")
        bootstrap = load_script("bootstrap_knpm", "bootstrap-knpm.py")
        names = [name for name, _version, _depends in builder.PACKAGES_TO_BUILD]
        registered = [name for name, _version, _filename in bootstrap.BOOTSTRAP_PACKAGES]

        for name in (
            "iproute2-target",
            "dhcpcd-target",
            "iwd-target",
            "linux-firmware-wifi-target",
        ):
            with self.subTest(package=name):
                self.assertIn(name, names)
                self.assertIn(name, registered)

        self.assertLess(names.index("dbus-target"), names.index("iwd-target"))
        self.assertLess(names.index("readline-target"), names.index("iwd-target"))
        self.assertLess(names.index("openssl-target"), names.index("iwd-target"))

        assembly = (ROOT / "scripts" / "assemble-rootfs.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"$SYSROOT/usr/bin/iwctl"', assembly)
        self.assertIn('"$SYSROOT/usr/libexec/iwd"', assembly)
        self.assertIn('"$SYSROOT/usr/sbin/dhcpcd"', assembly)
        self.assertIn('cat > "$ROOTFS/etc/init.d/iwd"', assembly)
        self.assertIn('need dbus', assembly)
        self.assertIn('EnableNetworkConfiguration=false', assembly)

    def test_wireless_kernel_drivers_and_tethering_are_configured(self) -> None:
        kernel_recipe = (ROOT / "recipes" / "linux-kernel.toml").read_text(
            encoding="utf-8"
        )
        for option in (
            "--module CFG80211",
            "--module MAC80211",
            "--module IWLWIFI",
            "--module ATH9K",
            "--module BRCMFMAC",
            "--module RTW89_8852AE",
            "--module MT7921E",
            "--module USB_NET_RNDIS_HOST",
        ):
            with self.subTest(option=option):
                self.assertIn(option, kernel_recipe)


if __name__ == "__main__":
    unittest.main()
