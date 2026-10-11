"""Machine-policy-aware environment verification.

An intentionally disabled provider must be SKIPPED_BY_POLICY, never an
environment failure; a configured machine must still probe Ollama. No real
network access: urllib is mocked.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import pathlib
import sys
import unittest
import unittest.mock
import urllib.error


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))


def load_verify_environment():
    loader = importlib.machinery.SourceFileLoader(
        "verify_environment", str(ROOT / "scripts/verify-environment"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


VERIFY = load_verify_environment()


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(env):
    out, err = io.StringIO(), io.StringIO()
    with unittest.mock.patch.dict("os.environ", env, clear=False), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = VERIFY.main()
    return code, out.getvalue(), err.getvalue()


class MachinePolicyTests(unittest.TestCase):
    def test_work_mac_policy_skips_probe_and_passes(self) -> None:
        with unittest.mock.patch.object(VERIFY.urllib.request, "urlopen") as probe:
            code, out, _ = _run({"LAI_MACHINE": "work-mac"})
        probe.assert_not_called()
        self.assertEqual(0, code)
        self.assertIn("Ollama: NOT_CONFIGURED_BY_MACHINE_POLICY", out)
        self.assertIn("Ollama health: SKIPPED_BY_POLICY", out)

    def test_configured_machine_still_probes_and_unreachable_fails(self) -> None:
        error = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        with unittest.mock.patch.object(VERIFY.urllib.request, "urlopen", side_effect=error) as probe:
            code, out, err = _run({"LAI_MACHINE": "home-wsl"})
        probe.assert_called_once()
        self.assertEqual(1, code)
        self.assertIn("Ollama health unavailable", err)
        self.assertNotIn("SKIPPED_BY_POLICY", out)

    def test_configured_machine_health_pass(self) -> None:
        with unittest.mock.patch.object(VERIFY.urllib.request, "urlopen", return_value=_Response()):
            code, out, _ = _run({"LAI_MACHINE": "home-wsl"})
        self.assertEqual(0, code)
        self.assertIn("Ollama health: HTTP 200", out)

    def test_unknown_machine_is_not_treated_as_policy(self) -> None:
        with unittest.mock.patch.object(VERIFY.urllib.request, "urlopen", return_value=_Response()) as probe:
            code, _out, _ = _run({"LAI_MACHINE": "does-not-exist"})
        probe.assert_called_once()
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
