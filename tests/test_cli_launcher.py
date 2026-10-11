"""Supported-Python CLI launcher: one selection policy for user-facing CLIs.

Proves the macOS portability failure mode (``env python3`` resolves to Python 3.9
while a newer interpreter is installed) is fixed for ``orchestrate``/``learn``/
``models``/``route`` without access to a real 3.9 host, and that the
no-supported-Python case fails clearly instead of raising a tomllib import error.

Everything is deterministic: fake interpreter fixtures on a private PATH, no real
interpreter or provider is downloaded, installed, or called.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRYPOINTS = {
    "orchestrate": ROOT / "scripts/orchestrate",
    "learn": ROOT / "scripts/learn",
    "models": ROOT / "scripts/models",
    "route": ROOT / "scripts/route",
}
MODULES = {name: ROOT / "scripts" / "common" / f"{name}_cli.py" for name in ENTRYPOINTS}


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

    def minimal_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "LAI_PYTHON"}
        env["PATH"] = str(self.fakebin)
        return env

    def run_entry(self, name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ENTRYPOINTS[name]), *args], text=True, capture_output=True, env=env)

    def sentinel_lines(self) -> list[str]:
        return self.sentinel.read_text(encoding="utf-8").splitlines()


class DiscoveryTests(LauncherTestBase):
    def test_old_python3_is_skipped_for_versioned_interpreter_all_entrypoints(self) -> None:
        self.write_interpreter("python3", supported=False)
        self.write_interpreter("python3.13", supported=True)
        for name in ENTRYPOINTS:
            with self.subTest(entrypoint=name):
                if self.sentinel.exists():
                    self.sentinel.unlink()
                result = self.run_entry(name, "subcommand", "--flag", env=self.minimal_env())
                self.assertEqual(0, result.returncode, result.stderr)
                lines = self.sentinel_lines()
                self.assertEqual(f"ARG={MODULES[name]}", lines[1])
                self.assertEqual(["ARG=subcommand", "ARG=--flag"], lines[2:])

    def test_launcher_does_not_mutate_path(self) -> None:
        self.write_interpreter("python3", supported=True)
        env = self.minimal_env()
        self.run_entry("orchestrate", "status", env=env)
        self.assertEqual(f"PATH={env['PATH']}", self.sentinel_lines()[0])

    def test_no_supported_python_fails_clearly_and_never_uses_uv(self) -> None:
        self.write_interpreter("python3", supported=False)
        uv_sentinel = self.home / "uv-ran"
        uv = self.fakebin / "uv"
        uv.write_text(f"#!/bin/sh\ntouch {uv_sentinel}\nexit 0\n", encoding="utf-8")
        uv.chmod(0o755)
        for name in ENTRYPOINTS:
            with self.subTest(entrypoint=name):
                if self.sentinel.exists():
                    self.sentinel.unlink()
                result = self.run_entry(name, "status", env=self.minimal_env())
                self.assertEqual(127, result.returncode, result.stderr)
                self.assertIn("3.11", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.sentinel.exists())
        self.assertFalse(uv_sentinel.exists(), "launcher must not invoke uv (no implicit download)")


class OverrideTests(LauncherTestBase):
    def test_supported_override_preserves_args_and_exit_code(self) -> None:
        good = self.write_interpreter("goodpy", supported=True, exit_code=7)
        env = os.environ | {"LAI_PYTHON": str(good)}
        for name in ENTRYPOINTS:
            with self.subTest(entrypoint=name):
                if self.sentinel.exists():
                    self.sentinel.unlink()
                result = self.run_entry(name, "doctor", "--json", env=env)
                self.assertEqual(7, result.returncode, result.stderr)
                self.assertEqual(f"ARG={MODULES[name]}", self.sentinel_lines()[1])

    def test_unsupported_override_fails_clearly(self) -> None:
        bad = self.write_interpreter("badpy", supported=False)
        env = os.environ | {"LAI_PYTHON": str(bad)}
        result = self.run_entry("orchestrate", "status", env=env)
        self.assertEqual(127, result.returncode)
        self.assertIn("LAI_PYTHON", result.stderr)
        self.assertIn("3.11", result.stderr)


class NoRecursionTests(unittest.TestCase):
    def test_entrypoints_are_sh_wrappers_over_distinct_modules(self) -> None:
        for name, entry in ENTRYPOINTS.items():
            with self.subTest(entrypoint=name):
                self.assertTrue(entry.exists(), entry)
                first = entry.read_bytes().split(b"\n", 1)[0]
                self.assertEqual(b"#!/bin/sh", first)
                module = MODULES[name]
                self.assertTrue(module.exists(), module)
                self.assertNotEqual(entry, module)
                self.assertIn(module.name.encode(), entry.read_bytes())

    def test_bootstrap_delegates_to_shared_launcher(self) -> None:
        bootstrap = (ROOT / "scripts/bootstrap").read_bytes()
        self.assertEqual(b"#!/bin/sh", bootstrap.split(b"\n", 1)[0])
        self.assertIn(b"common/launch", bootstrap)
        self.assertIn(b"common/bootstrap_cli.py", bootstrap)


class SupportedPythonTests(LauncherTestBase):
    def test_system_supported_python_runs_real_cli(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("test runner Python is unsupported")
        env = {k: v for k, v in os.environ.items() if k != "LAI_PYTHON"}
        for name in ENTRYPOINTS:
            with self.subTest(entrypoint=name):
                result = self.run_entry(name, "--help", env=env)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
