from __future__ import annotations

import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import bootstrap  # noqa: E402
import doctor  # noqa: E402


VERSIONS = {
    "opencode": "1.18.35",
    "codex": "codex-cli 0.159.3",
    "rtk": "rtk 0.49.0",
    "graphify": "graphify 0.9.65",
    "headroom": "headroom, version 0.37.0",
}

FAKE_TOOL = textwrap.dedent(
    f"""\
    #!/usr/bin/env python3
    import os, pathlib, sys
    name = pathlib.Path(sys.argv[0]).name
    args = sys.argv[1:]
    versions = {VERSIONS!r}
    if args == ["--version"]:
        override = os.environ.get("FAKE_VERSION_" + name.upper())
        print(override or versions.get(name, "0.0.0"))
        sys.exit(0)
    sentinel = os.environ.get("FAKE_SENTINEL")
    if sentinel:
        pathlib.Path(sentinel).write_text(name + " " + " ".join(args))
    sys.exit(3)
    """
)

OC_WRAPPER = textwrap.dedent(
    """\
    #!/bin/bash
    if [ -n "${FAKE_SENTINEL:-}" ]; then echo oc > "$FAKE_SENTINEL"; fi
    exit 0
    """
)


def run_git(path: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    delay = 0.0

    def do_GET(self) -> None:  # noqa: N802
        if self.delay:
            time.sleep(self.delay)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:  # silence
        pass


def start_health_server(delay: float = 0.0) -> tuple[http.server.HTTPServer, int]:
    handler = type("_Handler", (_HealthHandler,), {"delay": delay})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_port


class DoctorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        bootstrap.detect_graphify_version.cache_clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name in VERSIONS:
            self._write_tool(name)
        # Isolated PATH: only fake tools plus the system binaries doctor/git need,
        # so deleting a fake cannot silently fall back to a host-installed tool.
        self.iso = self.home / "iso"
        self.iso.mkdir()
        for name in ("git", "sh", "env"):
            target = shutil.which(name)
            if target:
                (self.iso / name).symlink_to(target)
        (self.iso / "python3").symlink_to(sys.executable)
        self.wrapper = self.home / ".local" / "bin" / "oc"
        self.wrapper.parent.mkdir(parents=True, exist_ok=True)
        self.wrapper.write_text(OC_WRAPPER, encoding="utf-8")
        self.wrapper.chmod(0o755)
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True)
        (self.codex / "RTK.md").write_text("# RTK\n", encoding="utf-8")
        (self.codex / "AGENTS.md").write_text("@RTK.md\n", encoding="utf-8")
        (self.codex / "config.toml").write_text(
            "[mcp_servers.headroom]\n"
            f'command = "{self.bin / "headroom"}"\n'
            'args = ["mcp", "serve"]\n\n'
            "[projects.demo]\n"
            'trust_level = "trusted"\n',
            encoding="utf-8",
        )
        self.repo = self.home / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init")
        run_git(self.repo, "config", "user.email", "t@example.com")
        run_git(self.repo, "config", "user.name", "t")
        (self.repo / "file.py").write_text("x = 1\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "init")
        self.head = run_git(self.repo, "rev-parse", "HEAD")
        self.identity = "remote:https://example.com/o/r.git"
        self.graph_dir = self.home / "graphs" / "graphify-out"
        self.health, self.port = start_health_server()
        self.env = os.environ | {
            "HOME": str(self.home),
            "LAI_STATE_DIR": str(self.state),
            "LAI_MACHINE": "work-mac",
            "PATH": str(self.iso) + os.pathsep + str(self.bin),
        }
        self.write_local()
        self.make_graph()

    def tearDown(self) -> None:
        self.health.shutdown()
        self.health.server_close()
        self.tmp.cleanup()

    def _write_tool(self, name: str) -> None:
        path = self.bin / name
        path.write_text(FAKE_TOOL, encoding="utf-8")
        path.chmod(0o755)

    def write_local(self, extra: dict | None = None) -> None:
        local = {
            "repository": {"name": "repo", "identity": self.identity,
                           "root": str(self.repo), "head": self.head},
            "graphify": {"graph_dir": str(self.graph_dir), "binary": "graphify"},
            "headroom": {"host": "127.0.0.1", "port": self.port, "bin": str(self.bin / "headroom")},
        }
        if extra:
            for key, value in extra.items():
                if isinstance(value, dict) and isinstance(local.get(key), dict):
                    local[key].update(value)
                else:
                    local[key] = value
        (self.state / "machine.local.yaml").write_text(json.dumps(local), encoding="utf-8")

    def make_graph(self, head: str | None = None, version: str = "0.9.65") -> None:
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        graph = self.graph_dir / "graph.json"
        graph.write_text(json.dumps({"nodes": [{"id": "a"}], "links": []}), encoding="utf-8")
        repo = {"name": "repo", "identity": self.identity, "root": str(self.repo),
                "head": head or self.head}
        bootstrap.write_graph_manifest(self.graph_dir, repo, version, graph)

    def cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts/bootstrap"), *args], cwd=ROOT,
                              env=env or self.env, text=True, capture_output=True)

    def report(self, env: dict | None = None) -> dict:
        env = env or self.env
        with unittest.mock.patch.dict(os.environ, env):
            return doctor.build_report(env)

    def checks(self, report: dict, component: str, check: str) -> dict:
        return next(item for item in report["checks"]
                    if item["component"] == component and item["check"] == check)


class CommandTests(DoctorTestBase):
    def test_missing_machine_is_config_error(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "LAI_MACHINE"}
        result = self.cli("doctor", env=env)
        self.assertEqual(2, result.returncode)
        self.assertIn("LAI_MACHINE", result.stderr)

    def test_unknown_machine_is_config_error(self) -> None:
        result = self.cli("doctor", "--machine", "nope")
        self.assertEqual(2, result.returncode)

    def test_all_healthy_is_ready(self) -> None:
        report = self.report()
        self.assertTrue(report["ready"])
        self.assertEqual(0, report["summary"]["fail"])
        self.assertEqual(0, self.cli("doctor").returncode)

    def test_warn_only_exits_zero_and_stays_ready(self) -> None:
        (self.repo / "file.py").write_text("x = 2\n", encoding="utf-8")
        result = self.cli("doctor")
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(self.cli("doctor", "--json").stdout)
        self.assertTrue(report["ready"])
        self.assertGreater(report["summary"]["warn"], 0)
        self.assertEqual(0, report["summary"]["fail"])

    def test_fail_exits_one(self) -> None:
        (self.bin / "headroom").unlink()
        self.write_local({"headroom": {"port": 1}})
        result = self.cli("doctor")
        self.assertEqual(1, result.returncode, result.stderr)
        report = json.loads(self.cli("doctor", "--json").stdout)
        self.assertFalse(report["ready"])


class RepositoryTests(DoctorTestBase):
    def test_clean_repo_passes(self) -> None:
        report = self.report()
        self.assertEqual(doctor.PASS, self.checks(report, "repository", "worktree")["status"])

    def test_dirty_repo_warns(self) -> None:
        (self.repo / "file.py").write_text("x = 2\n", encoding="utf-8")
        report = self.report()
        self.assertEqual(doctor.WARN, self.checks(report, "repository", "worktree")["status"])
        self.assertTrue(report["ready"])


class VersionTests(DoctorTestBase):
    def test_exact_version_passes(self) -> None:
        report = self.report()
        self.assertEqual(doctor.PASS, self.checks(report, "codex", "version")["status"])

    def test_newer_version_warns_without_mutation(self) -> None:
        env = dict(self.env)
        env["FAKE_VERSION_OPENCODE"] = "1.19.0"
        report = self.report(env)
        check = self.checks(report, "opencode", "version")
        self.assertEqual(doctor.WARN, check["status"])
        self.assertEqual("1.19.0", check["observed"])
        self.assertEqual("1.18.35", check["expected"])

    def test_missing_required_binary_fails(self) -> None:
        (self.bin / "rtk").unlink()
        bootstrap.detect_graphify_version.cache_clear()
        report = self.report()
        self.assertEqual(doctor.FAIL, self.checks(report, "rtk", "binary")["status"])
        self.assertFalse(report["ready"])

    def test_optional_missing_binary_skips(self) -> None:
        (self.bin / "rtk").unlink()
        self.write_local({"doctor": {"components": {"rtk": {"required": False}}}})
        report = self.report()
        self.assertEqual(doctor.SKIP, self.checks(report, "rtk", "binary")["status"])
        self.assertNotIn(doctor.FAIL, [c["status"] for c in report["checks"]])


class BaselineTests(unittest.TestCase):
    def test_per_machine_override_resolves_with_default_fallback(self) -> None:
        opencode = doctor.load_baseline()["tools"]["opencode"]
        self.assertEqual("1.18.35", doctor._verified_version(opencode, "work-mac"))
        self.assertEqual("2.0.14", doctor._verified_version(opencode, "home-wsl"))
        self.assertEqual("1.18.35", doctor._verified_version(opencode, "unknown-machine"))
        self.assertEqual("1.18.35", doctor._verified_version(opencode, None))

    def test_capabilities_stay_separate_from_versions(self) -> None:
        for tool in doctor.load_baseline()["tools"].values():
            self.assertIn("capability", tool)
            self.assertNotIn("version", tool["capability"])


class GraphifyTests(DoctorTestBase):
    def test_current_passes(self) -> None:
        report = self.report()
        self.assertEqual(doctor.PASS, self.checks(report, "graphify", "freshness")["status"])

    def test_stale_warns(self) -> None:
        self.make_graph(head="b" * 40)
        report = self.report()
        self.assertEqual(doctor.WARN, self.checks(report, "graphify", "freshness")["status"])
        self.assertTrue(report["ready"])

    def test_blocked_fails(self) -> None:
        self.write_local({"graphify": {"graph_dir": str(self.repo / "graphify-out")}})
        report = self.report()
        self.assertEqual(doctor.FAIL, self.checks(report, "graphify", "freshness")["status"])
        self.assertFalse(report["ready"])

    def test_doctor_never_rebuilds_graph(self) -> None:
        sentinel = self.home / "sentinel"
        env = dict(self.env)
        env["FAKE_SENTINEL"] = str(sentinel)
        self.cli("doctor", env=env)
        self.assertFalse(sentinel.exists())


class HeadroomTests(DoctorTestBase):
    def test_healthy_passes(self) -> None:
        report = self.report()
        self.assertEqual(doctor.PASS, self.checks(report, "headroom", "health")["status"])

    def test_unavailable_required_fails(self) -> None:
        self.write_local({"headroom": {"port": 1}})
        report = self.report()
        self.assertEqual(doctor.FAIL, self.checks(report, "headroom", "health")["status"])
        self.assertFalse(report["ready"])

    def test_health_timeout_is_bounded(self) -> None:
        slow, port = start_health_server(delay=1.0)
        try:
            started = time.monotonic()
            ok = doctor.http_health("127.0.0.1", port, 0.2)
            elapsed = time.monotonic() - started
        finally:
            slow.shutdown()
            slow.server_close()
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.8)

    def test_doctor_never_starts_headroom(self) -> None:
        sentinel = self.home / "sentinel"
        env = dict(self.env)
        env["FAKE_SENTINEL"] = str(sentinel)
        self.cli("doctor", env=env)
        self.assertFalse(sentinel.exists())


class ReadOnlyTests(DoctorTestBase):
    def test_user_global_config_untouched(self) -> None:
        opencode = self.home / ".config" / "opencode"
        opencode.mkdir(parents=True)
        canary = opencode / "opencode.jsonc"
        canary.write_text('{"keep": true}\n', encoding="utf-8")
        codex_before = (self.codex / "config.toml").read_bytes()
        canary_before = canary.read_bytes()
        self.cli("doctor")
        self.assertEqual(canary_before, canary.read_bytes())
        self.assertEqual(codex_before, (self.codex / "config.toml").read_bytes())

    def test_no_state_or_backups_created(self) -> None:
        before = sorted(p.name for p in self.state.iterdir())
        self.cli("doctor")
        self.assertEqual(before, sorted(p.name for p in self.state.iterdir()))
        self.assertFalse((self.state / "backups").exists())

    def test_wrapper_is_never_executed(self) -> None:
        sentinel = self.home / "wrapper-ran"
        env = dict(self.env)
        env["FAKE_SENTINEL"] = str(sentinel)
        self.cli("doctor", env=env)
        self.assertFalse(sentinel.exists())


class SecurityTests(DoctorTestBase):
    def test_secret_never_printed(self) -> None:
        secret = "sk-" + "a" * 30
        env = dict(self.env)
        env["DEEPSEEK_API_KEY"] = secret
        env["OPENCODE_TOKEN"] = secret
        for args in (("doctor",), ("doctor", "--json")):
            result = self.cli(*args, env=env)
            self.assertNotIn(secret, result.stdout + result.stderr)


class JsonTests(DoctorTestBase):
    def test_deterministic_schema(self) -> None:
        report = json.loads(self.cli("doctor", "--json").stdout)
        self.assertEqual(["checks", "machine", "platform", "ready", "summary"], sorted(report))
        self.assertIsInstance(report["ready"], bool)
        self.assertEqual(["fail", "pass", "skip", "warn"], sorted(report["summary"]))
        for check in report["checks"]:
            self.assertEqual(["category", "check", "component", "expected", "observed",
                              "reason", "status"], sorted(check))
            self.assertIn(check["status"], (doctor.PASS, doctor.WARN, doctor.FAIL, doctor.SKIP))

    def test_ready_boolean_matches_fail_count(self) -> None:
        report = json.loads(self.cli("doctor", "--json").stdout)
        self.assertEqual(report["summary"]["fail"] == 0, report["ready"])


if __name__ == "__main__":
    unittest.main()
