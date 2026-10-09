"""Context Retrieval Optimization V0.1 tests: budgets, retrieval, dedup, Graphify
states, security, validation, and synthetic acceptance cases. No AI calls."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import context_retrieval as ctx  # noqa: E402


def _request(root, **kw):
    base = dict(task_id="T1", task_family="configuration_resolution",
                task_class="CORRECTNESS_SENSITIVE", executor_type="AI", context_budget="SMALL",
                objective_summary="review", acceptance_criteria=["ok"], repository={"head": "abc"})
    base.update(kw)
    return ctx.ContextRequest.build(**base)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "src.py").write_text(
            "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n" + "\n".join(
                f"# filler {i}" for i in range(200)) + "\n", encoding="utf-8")
        (self.root / "cfg.json").write_text(json.dumps(
            {"task_classes": {"STRONG": {"tier": "STRONG", "effort": "high"}},
             "token": "sk-" + "a" * 30}), encoding="utf-8")
        (self.root / "bin.dat").write_bytes(b"\x00\x01\x02binary")
        self.budgets = {"TINY": 120, "SMALL": 4000, "MEDIUM": 40000, "LARGE": 120000}

    def tearDown(self) -> None:
        self.tmp.cleanup()


class BudgetTests(Fixture):
    def test_limits_ordered(self) -> None:
        budgets = ctx.load_budgets()
        self.assertLess(budgets["TINY"], budgets["SMALL"])
        self.assertLess(budgets["SMALL"], budgets["MEDIUM"])
        self.assertLess(budgets["MEDIUM"], budgets["LARGE"])

    def test_pack_is_ceiling_not_padding(self) -> None:
        result = ctx.build_pack(_request(self.root), root=self.root, budgets=self.budgets)
        self.assertEqual(ctx.STATUS_READY, result["status"])
        self.assertLess(result["manifest"]["size"], self.budgets["SMALL"])

    def test_optional_dropped_before_required(self) -> None:
        request = _request(self.root, context_budget="TINY",
                           source_refs=[{"path": "src.py", "start": 1, "end": 200, "required": False}],
                           knowledge_refs=["runtime-state-local"])
        result = ctx.build_pack(request, root=self.root, budgets=self.budgets)
        self.assertLessEqual(result["manifest"]["size"], self.budgets["TINY"] + 1)

    def test_required_overflow_rejected(self) -> None:
        request = _request(self.root, context_budget="TINY",
                           source_refs=[{"path": "src.py", "start": 1, "end": 200, "required": True}])
        result = ctx.build_pack(request, root=self.root, budgets=self.budgets)
        self.assertEqual(ctx.STATUS_BUDGET, result["status"])
        self.assertFalse(result["manifest"]["required_fits"])

    def test_tool_only_builds_no_pack(self) -> None:
        result = ctx.build_pack(_request(self.root, executor_type="TOOL_ONLY"), root=self.root)
        self.assertIsNone(result["pack"])
        self.assertFalse(result["manifest"]["context_required"])


class RetrievalTests(Fixture):
    def test_explicit_file_range(self) -> None:
        result = ctx.build_pack(_request(self.root, source_refs=[{"path": "src.py", "start": 1, "end": 2}]),
                                root=self.root, budgets=self.budgets)
        self.assertEqual("FILE_RANGE", result["items"][-1]["source_kind"])
        self.assertIn("def alpha", result["items"][-1]["content"])

    def test_symbol_retrieval(self) -> None:
        result = ctx.build_pack(_request(self.root, symbol_refs=[{"path": "src.py", "symbol": "beta"}]),
                                root=self.root, budgets=self.budgets)
        self.assertEqual("SYMBOL", result["items"][-1]["source_kind"])
        self.assertIn("def beta", result["items"][-1]["content"])

    def test_structured_config(self) -> None:
        result = ctx.build_pack(_request(self.root, config_refs=[{"path": "cfg.json", "key": "task_classes.STRONG"}]),
                                root=self.root, budgets=self.budgets)
        item = result["items"][-1]
        self.assertEqual("CONFIG_VALUE", item["source_kind"])
        self.assertIn("STRONG", item["content"])
        self.assertNotIn("filler", item["content"])  # not the whole file

    def test_dependency_facts(self) -> None:
        result = ctx.build_pack(_request(self.root, dependency_facts=[
            {"task_id": "T0", "status": "SUCCESS", "head": "abc", "clean": True}]),
            root=self.root, budgets=self.budgets)
        self.assertTrue(any(i["source_kind"] == "DEPENDENCY_FACT" for i in result["items"]))

    def test_knowledge_selection(self) -> None:
        result = ctx.build_pack(_request(self.root, knowledge_refs=["runtime-state-local"]),
                                root=self.root, budgets=self.budgets)
        self.assertTrue(any(i["source_kind"] == "KNOWLEDGE" for i in result["items"]))

    def test_diff_aware(self) -> None:
        result = ctx.build_pack(_request(self.root, diff_refs=[{"path": "src.py", "start": 1, "end": 5}]),
                                root=self.root, budgets=self.budgets)
        self.assertTrue(any(i["source_kind"] == "DIFF" for i in result["items"]))

    def test_symbol_miss_is_missing_not_whole_repo(self) -> None:
        result = ctx.build_pack(_request(self.root, symbol_refs=[{"path": "src.py", "symbol": "nope"}]),
                                root=self.root, budgets=self.budgets)
        self.assertEqual(ctx.STATUS_MISSING, result["status"])


class DedupTests(Fixture):
    def test_identical_content_single_item(self) -> None:
        refs = [{"path": "src.py", "start": 1, "end": 2}, {"path": "src.py", "start": 1, "end": 2}]
        result = ctx.build_pack(_request(self.root, source_refs=refs), root=self.root, budgets=self.budgets)
        file_items = [i for i in result["items"] if i["source_kind"] == "FILE_RANGE"]
        self.assertEqual(1, len(file_items))
        self.assertGreater(result["manifest"]["duplicate_chars_avoided"], 0)

    def test_overlapping_ranges_merged(self) -> None:
        refs = [{"path": "src.py", "start": 1, "end": 4}, {"path": "src.py", "start": 3, "end": 6}]
        result = ctx.build_pack(_request(self.root, source_refs=refs), root=self.root, budgets=self.budgets)
        file_items = [i for i in result["items"] if i["source_kind"] == "FILE_RANGE"]
        self.assertEqual(1, len(file_items))
        self.assertEqual([1, 6], file_items[0]["range"])

    def test_repeated_dependency_facts(self) -> None:
        fact = {"task_id": "T0", "status": "SUCCESS", "head": "abc"}
        result = ctx.build_pack(_request(self.root, dependency_facts=[fact, dict(fact)]),
                                root=self.root, budgets=self.budgets)
        self.assertEqual(1, len([i for i in result["items"] if i["source_kind"] == "DEPENDENCY_FACT"]))


class GraphifyTests(Fixture):
    def test_current_graph_recorded(self) -> None:
        result = ctx.build_pack(_request(self.root), root=self.root, budgets=self.budgets,
                                graph_status=ctx.GRAPHIFY_CURRENT)
        self.assertEqual(ctx.GRAPHIFY_CURRENT, result["manifest"]["graphify_status"])
        self.assertFalse(result["manifest"]["fallback_used"])

    def test_stale_graph_falls_back_safely(self) -> None:
        result = ctx.build_pack(_request(self.root), root=self.root, budgets=self.budgets,
                                graph_status=ctx.GRAPHIFY_STALE)
        self.assertEqual(ctx.STATUS_READY, result["status"])  # deterministic fallback
        self.assertTrue(result["manifest"]["fallback_used"])
        self.assertFalse(result["manifest"]["graphify_used"])

    def test_missing_graph_falls_back(self) -> None:
        result = ctx.build_pack(_request(self.root), root=self.root, budgets=self.budgets,
                                graph_status=ctx.GRAPHIFY_MISSING)
        self.assertEqual(ctx.STATUS_READY, result["status"])
        self.assertTrue(result["manifest"]["fallback_used"])


class SecurityTests(Fixture):
    def test_path_traversal_rejected(self) -> None:
        result = ctx.build_pack(_request(self.root, source_refs=[{"path": "../etc/passwd", "start": 1, "end": 2}]),
                                root=self.root, budgets=self.budgets)
        self.assertEqual(ctx.STATUS_UNSAFE, result["status"])

    def test_binary_rejected(self) -> None:
        result = ctx.build_pack(_request(self.root, source_refs=[{"path": "bin.dat", "start": 1, "end": 2}]),
                                root=self.root, budgets=self.budgets)
        self.assertEqual(ctx.STATUS_MISSING, result["status"])

    def test_secret_redacted_and_absent(self) -> None:
        result = ctx.build_pack(_request(self.root, source_refs=[{"path": "cfg.json", "start": 1, "end": 40}]),
                                root=self.root, budgets=self.budgets)
        self.assertIn(ctx.REDACTED, result["items"][-1]["content"])
        self.assertNotIn("sk-" + "a" * 30, json.dumps(result["pack"]))
        self.assertNotIn("sk-" + "a" * 30, json.dumps(result["manifest"]))

    def test_path_safe_helper(self) -> None:
        with self.assertRaises(ctx.ContextError):
            ctx._safe_path(self.root, "/etc/passwd")
        with self.assertRaises(ctx.ContextError):
            ctx._safe_path(self.root, "~/secret")


class AcceptanceTests(Fixture):
    def test_A_config_only(self) -> None:
        result = ctx.build_pack(_request(self.root, config_refs=[
            {"path": "cfg.json", "key": "task_classes.STRONG"}]), root=self.root, budgets=self.budgets)
        self.assertEqual(1, result["manifest"]["item_count"] - 2)  # objective + acceptance + config

    def test_B_symbol(self) -> None:
        result = ctx.build_pack(_request(self.root, symbol_refs=[{"path": "src.py", "symbol": "alpha"}]),
                                root=self.root, budgets=self.budgets)
        self.assertTrue(any(i["source_kind"] == "SYMBOL" for i in result["items"]))

    def test_C_architecture_multiple_bounded(self) -> None:
        result = ctx.build_pack(_request(self.root, context_budget="MEDIUM", source_refs=[
            {"path": "src.py", "start": 1, "end": 5}, {"path": "cfg.json", "start": 1, "end": 5}]),
            root=self.root, budgets=self.budgets)
        self.assertEqual(2, len([i for i in result["items"] if i["source_kind"] == "FILE_RANGE"]))

    def test_D_dependency_compact(self) -> None:
        result = ctx.build_pack(_request(self.root, dependency_facts=[
            {"task_id": "T0", "branch": "main", "head": "abc", "clean": True}]),
            root=self.root, budgets=self.budgets)
        self.assertTrue(any(i["source_kind"] == "DEPENDENCY_FACT" for i in result["items"]))

    def test_F_duplicate_symbol_single_item(self) -> None:
        refs = [{"path": "src.py", "start": 1, "end": 2}, {"path": "src.py", "start": 1, "end": 2}]
        result = ctx.build_pack(_request(self.root, source_refs=refs), root=self.root, budgets=self.budgets)
        self.assertEqual(1, len([i for i in result["items"] if i["source_kind"] == "FILE_RANGE"]))

    def test_H_required_overflow_no_pack(self) -> None:
        result = ctx.build_pack(_request(self.root, context_budget="TINY", source_refs=[
            {"path": "src.py", "start": 1, "end": 200, "required": True}]), root=self.root, budgets=self.budgets)
        self.assertEqual(ctx.STATUS_BUDGET, result["status"])


class FailoverContextTests(Fixture):
    def test_same_request_same_fingerprint(self) -> None:
        request = _request(self.root, symbol_refs=[{"path": "src.py", "symbol": "alpha"}])
        a = ctx.build_pack(request, root=self.root, budgets=self.budgets)
        b = ctx.build_pack(request, root=self.root, budgets=self.budgets)
        self.assertEqual(a["manifest"]["manifest_fingerprint"], b["manifest"]["manifest_fingerprint"])

    def test_new_failure_fact_changes_fingerprint(self) -> None:
        base = _request(self.root, symbol_refs=[{"path": "src.py", "symbol": "alpha"}])
        with_fact = _request(self.root, symbol_refs=[{"path": "src.py", "symbol": "alpha"}],
                             dependency_facts=[{"task_id": "T0", "failure": "RATE_LIMIT"}])
        a = ctx.build_pack(base, root=self.root, budgets=self.budgets)
        b = ctx.build_pack(with_fact, root=self.root, budgets=self.budgets)
        self.assertNotEqual(a["manifest"]["manifest_fingerprint"], b["manifest"]["manifest_fingerprint"])


class ComparisonFixtureTests(Fixture):
    def test_selected_smaller_than_broad(self) -> None:
        broad = ctx.build_pack(_request(self.root, context_budget="LARGE", source_refs=[
            {"path": "src.py", "start": 1, "end": 200}]), root=self.root, budgets=self.budgets)
        selected = ctx.build_pack(_request(self.root, symbol_refs=[{"path": "src.py", "symbol": "alpha"}]),
                                  root=self.root, budgets=self.budgets)
        self.assertLess(selected["manifest"]["size"], broad["manifest"]["size"])
        # deterministic measurements only (no token/cost percentages)
        self.assertGreater(broad["manifest"]["size"], 0)
        self.assertGreater(selected["manifest"]["size"], 0)


if __name__ == "__main__":
    unittest.main()
