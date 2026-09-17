from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


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

    def test_studio_does_not_change_qwen(self) -> None:
        original = self.qwen.read_text(encoding="utf-8")
        result = self.run_cli("use", "studio")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(original, self.qwen.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
