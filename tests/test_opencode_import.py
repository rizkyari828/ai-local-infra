from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import infra  # noqa: E402
import learning  # noqa: E402
import telemetry  # noqa: E402

IMPORTER = ROOT / "scripts/import-opencode-stats"


def stats_payload() -> dict:
    return {
        "models": [
            {
                "model": {"providerID": "deepseek", "id": "deepseek-flash", "variant": "high"},
                "tokens": {"input": 608374, "output": 114617, "reasoning": 151205,
                           "cache": {"read": 26471808, "write": 0}},
                "cost": 0.330164724,
                "sessions": 3, "subagents": 1, "prompts": 10, "steps": 42,
            },
            {
                "model": {"providerID": "deepseek", "id": "deepseek-flash", "variant": "low"},
                "tokens": {"input": 100, "output": 50, "reasoning": 0,
                           "cache": {"read": 900, "write": 0}},
                "cost": 0.0001,
                "sessions": 1, "subagents": 0, "prompts": 2, "steps": 4,
            },
            {
                "model": {"providerID": "deepseek", "id": "deepseek-flash", "variant": "default"},
                "tokens": {"input": 10, "output": 5, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}},
                "cost": 0.00001,
                "sessions": 1, "subagents": 0, "prompts": 1, "steps": 1,
            },
        ]
    }


class ImportCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(IMPORTER), *args], cwd=ROOT, text=True, capture_output=True)

    def _payload_file(self, temp: str, payload: object) -> pathlib.Path:
        path = pathlib.Path(temp) / "stats.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return path

    def test_valid_stats_import_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload_file(temp, stats_payload())
            output = pathlib.Path(temp) / "stats.jsonl"
            result = self._run("--input", str(payload), "--output", str(output), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            summary = json.loads(result.stdout)
            self.assertTrue(summary["appended"])
            self.assertEqual(3, summary["rows"])
            records = learning.load_jsonl(output)
            self.assertEqual(3, len(records))
            for record in records:
                telemetry.validate_opencode_stats_event(record)
            high = next(r for r in records if r["variant"] == "high")
            self.assertEqual("deepseek", high["provider"])
            self.assertEqual("deepseek-flash", high["model"])
            self.assertEqual(26471808, high["cached_input_tokens"])
            self.assertEqual(608374, high["cache_miss_tokens"])
            self.assertEqual(27080182, high["input_tokens"])
            self.assertEqual(27194799, high["total_tokens"])
            self.assertEqual(114617, high["output_tokens"])
            self.assertEqual(151205, high["reasoning_tokens"])
            self.assertEqual(0, high["cache_write_tokens"])
            self.assertAlmostEqual(0.9775, high["cache_hit_ratio"])
            self.assertAlmostEqual(0.330164724, high["provider_reported_cost"])
            self.assertEqual(3, high["sessions"])
            self.assertEqual("opencode", high["agent_client"])

    def test_high_low_default_variants_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload_file(temp, stats_payload())
            output = pathlib.Path(temp) / "stats.jsonl"
            self._run("--input", str(payload), "--output", str(output))
            records = learning.load_jsonl(output)
            self.assertEqual({"high", "low", "default"}, {r["variant"] for r in records})
            summary = telemetry.summarize_opencode_usage(records)
            self.assertEqual(3, len(summary["by_provider_model_variant"]))
            self.assertEqual(1, summary["snapshot_count"])

    def test_missing_optional_fields_are_null(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload_file(
                temp, {"models": [{"model": {"providerID": "deepseek", "id": "deepseek-flash"}}]})
            output = pathlib.Path(temp) / "stats.jsonl"
            result = self._run("--input", str(payload), "--output", str(output), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            record = learning.load_jsonl(output)[0]
            telemetry.validate_opencode_stats_event(record)
            for field in ("variant", "input_tokens", "output_tokens", "reasoning_tokens",
                          "cached_input_tokens", "cache_write_tokens", "cache_miss_tokens",
                          "total_tokens", "cache_hit_ratio", "provider_reported_cost", "cost",
                          "sessions", "subagents", "prompts", "steps"):
                self.assertIsNone(record.get(field), field)

    def test_flat_single_row_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload_file(temp, {
                "providerID": "deepseek", "modelID": "deepseek-flash", "variant": "high",
                "tokens": {"input": 5, "output": 2, "cache": {"read": 95, "write": 0}},
                "cost": 0.001,
            })
            output = pathlib.Path(temp) / "stats.jsonl"
            self._run("--input", str(payload), "--output", str(output))
            record = learning.load_jsonl(output)[0]
            self.assertEqual(100, record["input_tokens"])
            self.assertEqual(95, record["cached_input_tokens"])

    def test_malformed_json_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload_file(temp, "{not json")
            output = pathlib.Path(temp) / "stats.jsonl"
            result = self._run("--input", str(payload), "--output", str(output))
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_command_unavailable_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "stats.jsonl"
            result = self._run("--opencode-path", "/nonexistent/opencode-xyz", "--output", str(output))
            self.assertEqual(1, result.returncode)
            self.assertIn("command unavailable", result.stderr)
            self.assertFalse(output.exists())

    def test_command_non_zero_exit_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "stats.jsonl"
            result = self._run("--opencode-path", "/bin/false", "--output", str(output))
            self.assertEqual(1, result.returncode)
            self.assertIn("command exited", result.stderr)
            self.assertFalse(output.exists())

    def test_idempotent_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload_file(temp, stats_payload())
            output = pathlib.Path(temp) / "stats.jsonl"
            first = self._run("--input", str(payload), "--output", str(output), "--json")
            second = self._run("--input", str(payload), "--output", str(output), "--json")
            self.assertTrue(json.loads(first.stdout)["appended"])
            duplicate = json.loads(second.stdout)
            self.assertFalse(duplicate["appended"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(3, len(learning.load_jsonl(output)))

    def test_no_prompt_or_secret_persistence(self) -> None:
        payload = stats_payload()
        payload["models"][0]["prompt"] = "SECRET-PROMPT-CONTENT"
        payload["models"][0]["message"] = "source code here"
        with tempfile.TemporaryDirectory() as temp:
            source = self._payload_file(temp, payload)
            output = pathlib.Path(temp) / "stats.jsonl"
            self._run("--input", str(source), "--output", str(output))
            text = output.read_text(encoding="utf-8")
            self.assertNotIn("SECRET-PROMPT-CONTENT", text)
            self.assertNotIn("source code here", text)
            for record in learning.load_jsonl(output):
                self.assertNotIn("prompt", record)
                self.assertNotIn("message", record)


class ValidationTests(unittest.TestCase):
    def test_schema_matches_fields(self) -> None:
        schema = infra.load_json(ROOT / "telemetry/schemas/opencode-stats.schema.json")
        self.assertEqual(set(schema["properties"]), set(telemetry.OPENCODE_STATS_FIELDS))
        self.assertEqual(set(schema["required"]), set(telemetry.OPENCODE_STATS_REQUIRED))

    def test_secret_like_source_rejected(self) -> None:
        record = {
            "schema_version": telemetry.OPENCODE_STATS_SCHEMA_VERSION,
            "snapshot_id": "sha256:" + "a" * 64,
            "captured_at": "2026-09-18T10:00:00Z",
            "provider": "deepseek",
            "model": "deepseek-flash",
            "source": "api_key=SuperSecret12345",
        }
        with self.assertRaises(infra.InfraError):
            telemetry.validate_opencode_stats_event(record)


class SummarizeTests(unittest.TestCase):
    def _record(self, variant: str, captured: str, **tokens: object) -> dict:
        base = {
            "provider": "deepseek", "model": "deepseek-flash", "variant": variant,
            "snapshot_id": "sha256:" + variant[0] * 64, "captured_at": captured,
            "input_tokens": None, "output_tokens": None, "cached_input_tokens": None,
            "cache_miss_tokens": None, "cache_write_tokens": None, "reasoning_tokens": None,
            "estimated_total_cost": None, "provider_reported_cost": None,
        }
        base.update(tokens)
        return base

    def test_latest_snapshot_per_variant_not_double_counted(self) -> None:
        records = [
            self._record("high", "2026-09-18T09:00:00Z", input_tokens=100, cached_input_tokens=900,
                         cache_miss_tokens=100, output_tokens=50, provider_reported_cost=0.01),
            self._record("high", "2026-09-18T10:00:00Z", input_tokens=200, cached_input_tokens=1800,
                         cache_miss_tokens=200, output_tokens=80, provider_reported_cost=0.02),
            self._record("low", "2026-09-18T10:00:00Z", input_tokens=10, cached_input_tokens=0,
                         cache_miss_tokens=10, output_tokens=5, provider_reported_cost=0.001),
        ]
        summary = telemetry.summarize_opencode_usage(records)
        self.assertEqual(2, summary["snapshot_count"])
        high = next(g for g in summary["by_provider_model_variant"] if g["variant"] == "high")
        self.assertEqual(1800, high["cached_input_tokens"])
        self.assertEqual(200, high["cache_miss_tokens"])
        self.assertAlmostEqual(0.02, high["provider_reported_cost"])
        self.assertAlmostEqual(0.021, summary["provider_reported_cost"])
        self.assertAlmostEqual(0.9, high["cache_hit_ratio"])

    def test_cache_metrics_reads_opencode_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "stats.jsonl"
            path.write_text(json.dumps(self._record(
                "high", "2026-09-18T10:00:00Z", input_tokens=608374, cached_input_tokens=26471808,
                cache_miss_tokens=608374, output_tokens=114617,
                provider_reported_cost=0.330164724)) + "\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/cache-metrics"), "--opencode", str(path)],
                cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("OpenCode stats", result.stdout)
            self.assertIn("deepseek-flash#high", result.stdout)
            self.assertIn("0.330165", result.stdout)


if __name__ == "__main__":
    unittest.main()
