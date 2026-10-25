from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kbuild  # noqa: E402


class RecipeConfigurationTests(unittest.TestCase):
    def test_every_recipe_and_dependency_graph_loads(self) -> None:
        recipes = sorted(kbuild.RECIPES.glob("*.toml"))
        self.assertGreater(len(recipes), 0, "no KBuild recipes found")

        for recipe_path in recipes:
            with self.subTest(recipe=recipe_path.name):
                recipe = kbuild.load_recipe(recipe_path.stem)
                self.assertEqual(recipe["name"], recipe_path.stem)
                self.assertIn(recipe.get("source_type", "archive"), ("archive", "file"))

    def test_ca_bundle_uses_a_pinned_single_file_source(self) -> None:
        recipe = kbuild.load_recipe("ca-certificates-target")
        self.assertEqual(recipe["source_type"], "file")
        self.assertEqual(recipe["version"], "2026.10.20")
        self.assertNotIn("archive_root", recipe)

    def test_curl_keeps_public_suffix_protection_enabled(self) -> None:
        curl = kbuild.load_recipe("curl-target")
        self.assertIn("libpsl-target", curl["depends"])
        psl = kbuild.load_recipe("libpsl-target")
        self.assertEqual(psl["version"], "0.23.3")
        self.assertIn("libidn2-target", psl["depends"])

    def test_remote_client_has_runtime_network_dependencies(self) -> None:
        recipe = kbuild.load_recipe("knpm-target")
        self.assertIn("curl-target", recipe["depends"])
        self.assertIn("ca-certificates-target", recipe["depends"])


    def test_iwd_has_openrc_compatible_cross_build_configuration(self) -> None:
        recipe = kbuild.load_recipe("iwd-target")
        self.assertEqual(recipe["version"], "3.12")
        self.assertIn("dbus-target", recipe["depends"])
        self.assertIn("openssl-target", recipe["depends"])
        self.assertIn("readline-target", recipe["depends"])

        build = "\n".join(recipe.get("build", []))
        self.assertIn("--disable-systemd-service", build)
        self.assertIn("--with-dbus-datadir=/usr/share", build)
        self.assertIn("PKG_CONFIG_SYSROOT_DIR=", build)
        self.assertIn('make -j"$(nproc)"', build)

        install = "\\n".join(recipe.get("install", []))
        self.assertIn("net.connman.iwd.service", install)
        self.assertIn("[D-BUS Service]", install)
        self.assertIn("Name=net.connman.iwd", install)
        self.assertIn("Exec=/usr/libexec/iwd", install)
        self.assertIn("User=root", install)

    def test_wifi_firmware_source_is_pinned(self) -> None:
        recipe = kbuild.load_recipe("linux-firmware-wifi-target")
        self.assertEqual(recipe["version"], "20261019")
        self.assertEqual(
            recipe["source"],
            "https://cdn.kernel.org/pub/linux/kernel/firmware/linux-firmware-20261019.tar.xz",
        )
        self.assertEqual(
            recipe["sha256"],
            "a10d1ab5baf2c74ba92e8c7cf313e691696c26bddfa6df00db795e48d70cd4ca",
        )

        install_commands = "\\n".join(recipe.get("install", []))
        self.assertIn("intel/iwlwifi", install_commands)
        self.assertIn("if [[ -d LICENSES ]]", install_commands)
        self.assertIn("cp -a LICENSES/.", install_commands)


if __name__ == "__main__":
    unittest.main()
