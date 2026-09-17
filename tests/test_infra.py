from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import infra  # noqa: E402


def load_ai_profile():
    loader = importlib.machinery.SourceFileLoader("ai_profile", str(ROOT / "scripts/ai-profile"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


AI_PROFILE = load_ai_profile()


class FakeClock:
    """Deterministic clock/sleep pair so readiness waits never really sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class WaitForHealthTests(unittest.TestCase):
    def test_immediate_success_returns_without_retry(self) -> None:
        fake = FakeClock()
        calls: list[str] = []
        ready = infra.wait_for_health(
            "http://127.0.0.1:11434",
            check=lambda endpoint: calls.append(endpoint) or True,
            sleep=fake.sleep,
            clock=fake.monotonic,
        )
        self.assertTrue(ready)
        self.assertEqual(["http://127.0.0.1:11434"], calls)
        self.assertEqual([], fake.sleeps)

    def test_delayed_readiness_succeeds_within_deadline(self) -> None:
        fake = FakeClock()
        attempts: list[float] = []

        def check(_endpoint: str) -> bool:
            attempts.append(fake.monotonic())
            return len(attempts) >= 3

        ready = infra.wait_for_health(
            "http://127.0.0.1:11434", timeout=5.0, interval=0.5,
            check=check, sleep=fake.sleep, clock=fake.monotonic,
        )
        self.assertTrue(ready)
        self.assertEqual([0.0, 0.5, 1.0], attempts)
        self.assertEqual([0.5, 0.5], fake.sleeps)

    def test_permanent_failure_stops_at_deadline(self) -> None:
        fake = FakeClock()
        attempts: list[float] = []

        def check(_endpoint: str) -> bool:
            attempts.append(fake.monotonic())
            return False

        ready = infra.wait_for_health(
            "http://127.0.0.1:11434", timeout=2.0, interval=0.5,
            check=check, sleep=fake.sleep, clock=fake.monotonic,
        )
        self.assertFalse(ready)
        self.assertEqual(2.0, fake.now)
        self.assertEqual([0.0, 0.5, 1.0, 1.5, 2.0], attempts)


class ProfileApplyReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.qwen = base / "settings.json"
        self.ollama = base / "override.conf"
        self.state = base / "state"
        self.active = self.state / "active-profile.json"
        self.secret = "do-not-print-or-change"
        self.qwen.write_text(json.dumps({
            "$version": 4,
            "env": {"OLLAMA_ANTHROPIC_KEY": self.secret},
            "model": {"name": "old", "baseUrl": "http://old"},
            "modelProviders": {"anthropic": []},
        }), encoding="utf-8")
        self.ollama.write_text("original-override\n", encoding="utf-8")
        self.original_qwen = self.qwen.read_text(encoding="utf-8")
        self.original_ollama = self.ollama.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @contextlib.contextmanager
    def patched(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(unittest.mock.patch.object(infra, "QWEN_PATH", self.qwen))
            stack.enter_context(unittest.mock.patch.object(infra, "OLLAMA_PATH", self.ollama))
            stack.enter_context(unittest.mock.patch.object(infra, "STATE_DIR", self.state))
            stack.enter_context(unittest.mock.patch.object(infra, "ACTIVE_PATH", self.active))
            stack.enter_context(unittest.mock.patch.object(infra, "BACKUP_DIR", self.state / "backups"))
            stack.enter_context(unittest.mock.patch.dict(os.environ, {"LAI_SKIP_SYSTEMD": "1"}))
            yield

    def args(self, profile_id: str) -> argparse.Namespace:
        return argparse.Namespace(profile_id=profile_id, dry_run=False, unload=False)

    def test_marker_written_only_after_successful_readiness(self) -> None:
        with self.patched():
            self.assertEqual(0, AI_PROFILE.cmd_use(self.args("coding-routine")))
        marker = json.loads(self.active.read_text(encoding="utf-8"))
        self.assertEqual("coding-routine", marker["id"])
        self.assertIn("OLLAMA_CONTEXT_LENGTH=32768", self.ollama.read_text(encoding="utf-8"))

    def test_delayed_readiness_succeeds_without_rollback(self) -> None:
        fake = FakeClock()
        attempts: list[float] = []

        def check(_endpoint: str) -> bool:
            attempts.append(fake.monotonic())
            return len(attempts) >= 3

        original_wait = infra.wait_for_health

        def delayed(endpoint: str) -> bool:
            return original_wait(endpoint, check=check, sleep=fake.sleep, clock=fake.monotonic)

        with self.patched():
            with unittest.mock.patch.object(infra, "wait_for_health", side_effect=delayed):
                self.assertEqual(0, AI_PROFILE.cmd_use(self.args("coding-routine")))
        self.assertEqual(3, len(attempts))
        self.assertTrue(self.active.exists())
        self.assertIn("OLLAMA_CONTEXT_LENGTH=32768", self.ollama.read_text(encoding="utf-8"))

    def test_permanent_failure_rolls_back_and_leaves_no_marker(self) -> None:
        with self.patched():
            with unittest.mock.patch.object(infra, "wait_for_health", return_value=False):
                with self.assertRaises(infra.InfraError):
                    AI_PROFILE.cmd_use(self.args("coding-routine"))
        self.assertEqual(self.original_qwen, self.qwen.read_text(encoding="utf-8"))
        self.assertEqual(self.original_ollama, self.ollama.read_text(encoding="utf-8"))
        self.assertFalse(self.active.exists())


class ProfileCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.qwen = base / "settings.json"
        self.ollama = base / "override.conf"
        self.state = base / "state"
        self.secret = "do-not-print-or-change"
        self.qwen.write_text(json.dumps({
            "$version": 4,
            "env": {"OLLAMA_ANTHROPIC_KEY": self.secret},
            "model": {"name": "old", "baseUrl": "http://old"},
            "modelProviders": {"anthropic": []},
        }), encoding="utf-8")
        self.env = os.environ | {
            "LAI_QWEN_SETTINGS": str(self.qwen),
            "LAI_OLLAMA_OVERRIDE": str(self.ollama),
            "LAI_STATE_DIR": str(self.state),
            "LAI_SKIP_SYSTEMD": "1",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts/ai-profile"), *args], cwd=ROOT, env=self.env,
                              text=True, capture_output=True)

    def test_dry_run_has_no_side_effect_and_redacts_unmanaged_secret(self) -> None:
        before = self.qwen.read_text(encoding="utf-8")
        result = self.run_cli("use", "coding-routine", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.qwen.read_text(encoding="utf-8"))
        self.assertFalse(self.ollama.exists())
        self.assertNotIn(self.secret, result.stdout)
        self.assertIn("DRY RUN", result.stdout)

    def test_apply_is_idempotent_and_rollback_restores(self) -> None:
        original = self.qwen.read_text(encoding="utf-8")
        first = self.run_cli("use", "coding-routine")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertTrue((self.state / "active-profile.json").exists())
        applied = json.loads(self.qwen.read_text(encoding="utf-8"))
        self.assertEqual(self.secret, applied["env"]["OLLAMA_ANTHROPIC_KEY"])
        self.assertEqual("qwen3.6:27b-coding", applied["model"]["name"])
        verified = self.run_cli("verify", "--profile", "coding-routine")
        self.assertEqual(0, verified.returncode, verified.stderr)
        second = self.run_cli("use", "coding-routine")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("no action taken", second.stdout)
        rolled = self.run_cli("rollback")
        self.assertEqual(0, rolled.returncode, rolled.stderr)
        self.assertEqual(original, self.qwen.read_text(encoding="utf-8"))
        self.assertFalse(self.ollama.exists())
        self.assertFalse((self.state / "active-profile.json").exists())

    def test_studio_does_not_change_qwen(self) -> None:
        original = self.qwen.read_text(encoding="utf-8")
        result = self.run_cli("use", "studio")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(original, self.qwen.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
