"""Master A — managed guidance ownership and precedence.

Bootstrap owns declared managed blocks/keys and nothing else: unrelated user
instructions must survive every update, a dry-run must show the same proposed
changes it would apply, a restricted unit must be skipped rather than silently
applied, and a repeated apply must change nothing.

Every test uses a temporary HOME/state directory. No real user guidance file is
read or written.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import bootstrap  # noqa: E402

BLOCK_ID = "codex-guidance"


class GuidanceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True)
        self.agents = self.codex / "AGENTS.md"
        self.env = os.environ | {
            "HOME": str(self.home),
            "LAI_STATE_DIR": str(self.state),
            "LAI_MACHINE": "work-mac",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @contextlib.contextmanager
    def patched(self, **extra):
        merged = dict(self.env)
        merged.update(extra)
        with unittest.mock.patch.dict(os.environ, merged):
            yield merged

    def plan(self, **extra) -> dict:
        with self.patched(**extra):
            plan, _meta = bootstrap.build_plan(self.env)
        return {unit["id"]: unit for unit in plan}

    def apply(self, dry_run: bool = False, **extra) -> tuple[int, str]:
        out = io.StringIO()
        with self.patched(**extra), contextlib.redirect_stdout(out):
            code = bootstrap.cmd_apply(self.env, dry_run=dry_run)
        return code, out.getvalue()

    def write_local(self, values: dict) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "machine.local.yaml").write_text(json.dumps(values), encoding="utf-8")

    def markers(self) -> tuple[str, str]:
        return bootstrap.markers("html", BLOCK_ID)

    def write_agents(self, before: str, body: str, after: str) -> None:
        begin, end = self.markers()
        self.agents.write_text(f"{before}{begin}\n{body}\n{end}\n{after}", encoding="utf-8")


class ManagedBlockTests(GuidanceTestBase):
    def test_managed_block_is_updated_deterministically(self) -> None:
        self.write_agents("# Notes\n", "stale managed body\n", "")
        planned = self.plan()[f"codex.guidance"]
        self.assertEqual("DRIFTED", planned["state"])
        self.assertTrue(planned["changes"])
        # Deterministic: re-planning yields byte-identical proposed content.
        self.assertEqual(planned["write"], self.plan()["codex.guidance"]["write"])
        code, out = self.apply()
        self.assertEqual(0, code, out)
        self.assertIn("CHANGE codex.guidance", out)
        after = self.agents.read_text(encoding="utf-8")
        begin, end = self.markers()
        self.assertIn(begin, after)
        self.assertIn(end, after)
        self.assertNotIn("stale managed body", after)
        self.assertIn(planned["desired"].strip(), after)
        # Re-reading the applied file is aligned, not re-drifted.
        self.assertEqual("ALIGNED", self.plan()["codex.guidance"]["state"])

    def test_unrelated_user_content_survives(self) -> None:
        self.write_agents("# My own instructions\n@RTK.md\n\n", "old body\n", "\ntrailing user note\n")
        code, out = self.apply()
        self.assertEqual(0, code, out)
        after = self.agents.read_text(encoding="utf-8")
        self.assertIn("# My own instructions", after)
        self.assertIn("@RTK.md", after)
        self.assertIn("trailing user note", after)
        self.assertNotIn("old body", after)

    def test_dry_run_shows_changes_without_applying(self) -> None:
        self.write_agents("# Notes\n", "stale managed body\n", "")
        before = self.agents.read_bytes()
        code, out = self.apply(dry_run=True)
        self.assertEqual(0, code, out)
        self.assertIn("CHANGE codex.guidance", out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(before, self.agents.read_bytes())
        self.assertFalse((self.state / "bootstrap-applied.json").exists())

    def test_repeated_apply_is_idempotent(self) -> None:
        self.write_agents("# Notes\n", "stale managed body\n", "")
        first_code, _ = self.apply()
        first = self.agents.read_bytes()
        second_code, second_out = self.apply()
        self.assertEqual(0, first_code)
        self.assertEqual(0, second_code, second_out)
        self.assertIn("No action taken", second_out)
        self.assertEqual(first, self.agents.read_bytes())

    def test_restricted_scope_does_not_silently_apply_the_unit(self) -> None:
        self.write_agents("# Notes\n", "stale managed body\n", "")
        before = self.agents.read_bytes()
        self.write_local({"override_units": ["codex.guidance"]})
        planned = self.plan()
        self.assertEqual("LOCAL-OVERRIDE", planned["codex.guidance"]["state"])
        self.assertFalse(planned["codex.guidance"]["changes"])
        # An unrelated drifted managed unit is still applied, so the restriction is
        # per-unit and not a silent no-op.
        self.assertTrue(planned["graphify.runtime"]["changes"])
        code, out = self.apply()
        self.assertEqual(0, code, out)
        self.assertNotIn("CHANGE codex.guidance", out)
        self.assertEqual(before, self.agents.read_bytes())
        self.assertTrue((self.state / "graphify" / "runtime.json").exists())

    def test_malformed_markers_block_instead_of_guessing(self) -> None:
        begin, _end = self.markers()
        self.agents.write_text(f"{begin}\ndangling\n", encoding="utf-8")
        planned = self.plan()[f"codex.guidance"]
        self.assertEqual("BLOCKED", planned["state"])
        self.assertTrue(planned["hard"])
        code, out = self.apply()
        self.assertEqual(1, code)
        self.assertIn("BLOCKED", out)
        self.assertIn("dangling", self.agents.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
