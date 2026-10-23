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

    def test_remote_client_has_runtime_network_dependencies(self) -> None:
        recipe = kbuild.load_recipe("knpm-target")
        self.assertIn("curl-target", recipe["depends"])
        self.assertIn("ca-certificates-target", recipe["depends"])


if __name__ == "__main__":
    unittest.main()
