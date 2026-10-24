from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import kbuild


class BuildTimerTests(unittest.TestCase):
    def test_format_duration(self) -> None:
        cases = {
            0: "0s",
            65.9: "1m 5s",
            3600: "1h 0m 0s",
            90061: "1d 1h 1m 1s",
        }
        for elapsed, expected in cases.items():
            with self.subTest(elapsed=elapsed):
                self.assertEqual(kbuild._format_duration(elapsed), expected)

    def test_prompt_defaults_to_yes(self) -> None:
        stdin = Mock()
        stdin.isatty.return_value = True
        with patch.object(kbuild.sys, "stdin", stdin):
            with patch("builtins.input", return_value=""):
                self.assertTrue(kbuild._ask_build_timer())

    def test_prompt_accepts_no(self) -> None:
        stdin = Mock()
        stdin.isatty.return_value = True
        with patch.object(kbuild.sys, "stdin", stdin):
            with patch("builtins.input", return_value="N"):
                self.assertFalse(kbuild._ask_build_timer())

    def test_invalid_prompt_answer_is_retried(self) -> None:
        stdin = Mock()
        stdin.isatty.return_value = True
        output = io.StringIO()
        with patch.object(kbuild.sys, "stdin", stdin):
            with patch("builtins.input", side_effect=["maybe", "yes"]):
                with redirect_stdout(output):
                    self.assertTrue(kbuild._ask_build_timer())
        self.assertIn("Please answer 'y' or 'n'.", output.getvalue())

    def test_non_interactive_build_does_not_prompt(self) -> None:
        with patch.object(kbuild.sys, "stdin", io.StringIO("")):
            with patch("builtins.input") as input_mock:
                self.assertFalse(kbuild._ask_build_timer())
        input_mock.assert_not_called()

    def test_single_package_build_does_not_use_timer(self) -> None:
        args = SimpleNamespace(name="bash-target", dry_run=False, force=False)
        with patch.object(kbuild, "build") as build_mock:
            with patch.object(kbuild, "_ask_build_timer") as timer_mock:
                kbuild.command_build(args)
        build_mock.assert_called_once_with("bash-target", force=False)
        timer_mock.assert_not_called()

    def test_full_build_prints_elapsed_time_after_success(self) -> None:
        args = SimpleNamespace(name="all", dry_run=False, force=False)
        output = io.StringIO()
        with patch.object(kbuild, "all_recipe_order", return_value=list(kbuild.BOOTSTRAP_ORDER)):
            with patch.object(kbuild, "_ask_build_timer", return_value=True) as timer_mock:
                with patch.object(kbuild.time, "monotonic", side_effect=[100.0, 165.5]):
                    with patch.object(kbuild, "build"):
                        with patch.object(kbuild, "promote"):
                            with patch.object(kbuild, "deploy"):
                                with redirect_stdout(output):
                                    kbuild.command_build(args)

        timer_mock.assert_called_once_with()
        lines = output.getvalue().strip().splitlines()
        self.assertIn("KhaymOS build-all completed:", lines[-2])
        self.assertEqual(lines[-1], "Total KhaymOS build time: 1m 5s")

    def test_failed_full_build_does_not_print_completion_timer(self) -> None:
        args = SimpleNamespace(name="all", dry_run=False, force=False)
        output = io.StringIO()
        with patch.object(kbuild, "all_recipe_order", return_value=list(kbuild.BOOTSTRAP_ORDER)):
            with patch.object(kbuild, "_ask_build_timer", return_value=True):
                with patch.object(kbuild.time, "monotonic", return_value=100.0):
                    with patch.object(kbuild, "build", side_effect=kbuild.KBuildError("test failure")):
                        with redirect_stdout(output):
                            with self.assertRaises(kbuild.KBuildError):
                                kbuild.command_build(args)
        self.assertNotIn("Total KhaymOS build time:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
