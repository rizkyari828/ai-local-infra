"""Portable bootstrap launcher: selects a supported Python (>= 3.11).

Covers multi-machine portability without touching real PATH or a real Home WSL:
supported/unsupported explicit overrides, skipping an old ``python3`` for a
versioned interpreter, a deterministic error when nothing is supported, argument
and exit-code preservation, no PATH mutation, and no uv download/install attempt.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts/bootstrap"
CLI = ROOT / "scripts/common/bootstrap_cli.py"


def _interpreter_script(supported: bool, sentinel: pathlib.Path, exit_code: int) -> str:
    return (
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "if sys.argv[1:2] == ['-c']:\n"
        f"    sys.exit({'0' if supported else '1'})\n"
        f"sentinel = pathlib.Path({str(sentinel)!r})\n"
        "lines = ['PATH=' + os.environ.get('PATH', '')]\n"
        "lines += ['ARG=' + a for a in sys.argv[1:]]\n"
        "sentinel.write_text('\\n'.join(lines) + '\\n', encoding='utf-8')\n"
        f"sys.exit({exit_code})\n"
    )


class LauncherTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.sentinel = self.home / "sentinel"
        self.fakebin = self.home / "bin"
        self.fakebin.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_interpreter(self, name: str, *, supported: bool, exit_code: int = 0) -> pathlib.Path:
        path = self.fakebin / name
        path.write_text(_interpreter_script(supported, self.sentinel, exit_code), encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_launcher(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(LAUNCHER), *args], text=True, capture_output=True, env=env)

    def sentinel_lines(self) -> list[str]:
        return self.sentinel.read_text(encoding="utf-8").splitlines()


class OverrideTests(LauncherTestBase):
    def test_supported_override_preserves_args_and_exit_code(self) -> None:
        good = self.write_interpreter("goodpy", supported=True, exit_code=7)
        env = os.environ | {"LAI_PYTHON": str(good)}
        result = self.run_launcher("doctor", "--json", env=env)
        self.assertEqual(7, result.returncode, result.stderr)
        lines = self.sentinel_lines()
        self.assertEqual(f"ARG={CLI}", lines[1])
        self.assertEqual(["ARG=doctor", "ARG=--json"], lines[2:])

    def test_launcher_does_not_mutate_path(self) -> None:
        good = self.write_interpreter("goodpy", supported=True)
        env = os.environ | {"LAI_PYTHON": str(good)}
        self.run_launcher("status", env=env)
        self.assertIn(f"PATH={env['PATH']}", self.sentinel_lines())

    def test_unsupported_override_fails_clearly(self) -> None:
        bad = self.write_interpreter("badpy", supported=False)
        env = os.environ | {"LAI_PYTHON": str(bad)}
        result = self.run_launcher("status", env=env)
        self.assertEqual(127, result.returncode)
        self.assertIn("LAI_PYTHON", result.stderr)
        self.assertIn("3.11", result.stderr)
        self.assertFalse(self.sentinel.exists())

    def test_missing_override_fails_clearly(self) -> None:
        env = os.environ | {"LAI_PYTHON": str(self.home / "does-not-exist" / "python3")}
        result = self.run_launcher("status", env=env)
        self.assertEqual(127, result.returncode)
        self.assertIn("LAI_PYTHON", result.stderr)
        self.assertIn("3.11", result.stderr)


class DiscoveryTests(LauncherTestBase):
    def minimal_env(self) -> dict[str, str]:
        return os.environ | {"PATH": str(self.fakebin)}

    def test_old_python3_is_skipped_for_versioned_interpreter(self) -> None:
        self.write_interpreter("python3", supported=False)
        good = self.write_interpreter("python3.13", supported=True, exit_code=0)
        env = self.minimal_env()
        env.pop("LAI_PYTHON", None)
        result = self.run_launcher("graph", "status", env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"ARG={CLI}", self.sentinel_lines())
        self.assertTrue(good.exists())

    def test_no_supported_interpreter_is_deterministic_and_never_uses_uv(self) -> None:
        self.write_interpreter("python3", supported=False)
        uv_sentinel = self.home / "uv-ran"
        uv = self.fakebin / "uv"
        uv.write_text(f"#!/bin/sh\ntouch {uv_sentinel}\nexit 0\n", encoding="utf-8")
        uv.chmod(0o755)
        env = self.minimal_env()
        env.pop("LAI_PYTHON", None)
        result = self.run_launcher("status", env=env)
        self.assertEqual(127, result.returncode)
        self.assertIn("3.11", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(uv_sentinel.exists(), "launcher must not invoke uv (no implicit download)")
        self.assertFalse(self.sentinel.exists())


class SupportedPythonTests(LauncherTestBase):
    def test_launcher_runs_real_help_with_system_python(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("test runner Python is unsupported")
        env = {k: v for k, v in os.environ.items() if k != "LAI_PYTHON"}
        result = self.run_launcher("--help", env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_module_guard_rejects_old_python_without_tomllib_traceback(self) -> None:
        old = self._find_old_python()
        if old is None:
            self.skipTest("no pre-3.11 interpreter available to exercise the guard")
        result = subprocess.run([old, str(CLI), "status"], text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("requires Python >= 3.11", result.stderr)
        self.assertNotIn("tomllib", result.stderr)

    def _find_old_python(self) -> str | None:
        for name in ("python3.10", "python3.9", "python3"):
            path = pathlib.Path("/usr/bin") / name
            if not path.exists():
                continue
            probe = subprocess.run(
                [str(path), "-c", "import sys; print(sys.version_info[:2])"],
                text=True, capture_output=True)
            if probe.returncode == 0 and probe.stdout.strip() and tuple(
                    int(p) for p in probe.stdout.strip().strip("()").split(",")) < (3, 11):
                return str(path)
        return None


if __name__ == "__main__":
    unittest.main()
