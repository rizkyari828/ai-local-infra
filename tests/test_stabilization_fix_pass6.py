"""Positive-proof decomposition, exact source spans and intact worker delivery.

Offline adapters and temporary repositories only; no inference or provider calls.
"""

from __future__ import annotations

import copy
import hashlib
import unittest

from test_stabilization_fix_pass5 import Fixture, execution, orchestrator
import result_cache


NON_PLAIN = {
    "ASCII double quote": 'Fix output to exactly "alpha  and  beta"; update docs; run tests.',
    "curly quote": 'Fix output to exactly “alpha  and  beta”; update docs; run tests.',
    "ASCII single quote": "Fix output to exactly 'alpha and beta'; update docs; run tests.",
    "Unicode single quote": "Fix output to exactly ‘alpha and beta’; update docs; run tests.",
    "quoted comma": 'Fix output to exactly "alpha,beta"; update docs; run tests.',
    "quoted semicolon": 'Fix output to exactly "alpha;beta"; update docs; run tests.',
    "backtick": "Fix output to exactly `alpha && beta`; update docs; run tests.",
    "triple-backtick": "Fix code to:\n```python\nx = alpha, beta; gamma and delta\n```\n; run tests.",
    "tilde fence": "Fix code to:\n~~~python\nx = alpha, beta; gamma and delta\n~~~\n; run tests.",
    "indented code": "Fix code to:\n    x = alpha, beta\n    y = gamma and delta\n; run tests.",
    "tab": "Fix code to:\n\tx = alpha, beta\n; run tests.",
    "escaped quotes": r'Fix output to "alpha \"and\" beta"; update docs; run tests.',
    "multiple quotes": 'Fix output to "alpha,beta" and document ‘gamma;delta’; run tests.',
    "multiple separators": 'Fix output to "alpha, beta; gamma and delta"; run tests.',
    "unfamiliar Unicode": "Fix output to «alpha and beta»; update docs; run tests.",
    "code expression": "Fix result to f(alpha, beta) && gamma; run tests.",
    "unterminated": 'Fix output to "alpha and beta; update docs',
    "protected leftover": 'Fix output to "alpha,beta" and preserve legacy behavior; update docs',
}


class ConservativeDecompositionTests(Fixture):
    def assert_intact_delivery(self, objective):
        self.assertFalse(orchestrator.objective_is_provably_safe_for_decomposition(objective))
        plan = orchestrator.decompose(objective)
        self.assertTrue(plan["valid"], plan["diagnostics"])
        self.assertEqual(1, len(plan["nodes"]))
        node = plan["nodes"][0]
        self.assertEqual("AI", node["executor"])
        self.assertEqual(objective, node["objective_summary"])
        self.assertEqual(objective, node["task_contract"]["objective"])
        self.assertEqual([0, len(objective)], node["objective_span"])
        requests = self._probe(plan)
        self.assertEqual(1, len(requests))
        self.assertEqual(objective, requests[0]["task_contract"]["objective"])
        self.assertIn(f"Objective: {objective}\nOperation:", requests[0]["prompt"])

    def test_non_plain_cases_keep_exact_full_worker_objective(self):
        for label, objective in NON_PLAIN.items():
            with self.subTest(label=label):
                self.assert_intact_delivery(objective)

    def test_unknown_ascii_structure_is_not_plain_by_default(self):
        for objective in (
            "Fix result [alpha,beta]; update docs",
            "Fix result alpha/beta; run tests",
            "Fix output \\alpha; update docs",
            "Fix alpha\rupdate docs",
            "Fix output alpha  beta; run tests",
            "Fix output to exactly alpha and update tests",
            "Fix return value to alpha, beta; run tests",
            " Fix alpha; update docs",
            "Fix alpha; preserve legacy behavior",
        ):
            with self.subTest(objective=objective):
                self.assert_intact_delivery(objective)

    def test_gate_precedes_deterministic_fast_path(self):
        # Token coverage alone must not bypass the non-plain safety gate.
        self.assert_intact_delivery('Check current "HEAD" and current branch')

    def test_plain_examples_decompose_with_exact_spans(self):
        for objective in (
            "Update docs and run tests",
            "Review config; update docs; run tests",
            "Fix alpha and update tests",
        ):
            with self.subTest(objective=objective):
                self.assertTrue(orchestrator.objective_is_provably_safe_for_decomposition(objective))
                plan = orchestrator.decompose(objective)
                self.assertTrue(plan["valid"], plan["diagnostics"])
                self.assertGreater(len(plan["nodes"]), 1)
                spans = orchestrator._clause_spans(objective)
                self.assertEqual(sorted(spans), spans)
                for start, end, text in spans:
                    self.assertEqual(text, objective[start:end])
                    self.assertTrue(any(n["objective_span"] == [start, end] for n in plan["nodes"]))
                for node in plan["nodes"]:
                    start, end = node["objective_span"]
                    self.assertEqual(objective[start:end], node["task_contract"]["objective"])
                for request in self._probe(plan):
                    start, end = request["task_contract"]["source_span"]
                    self.assertIn(f"Objective: {objective[start:end]}\nOperation:", request["prompt"])

    def test_unassigned_plain_clause_falls_back_without_joining(self):
        # Both clauses satisfy the grammar, but the existing stages cannot own two fixes.
        objective = "Fix alpha and fix beta; update docs; run tests"
        self.assertTrue(orchestrator.objective_is_provably_safe_for_decomposition(objective))
        plan = orchestrator.decompose(objective)
        self.assertEqual([objective], [n["task_contract"]["objective"] for n in plan["nodes"]])
        self.assertIn(f"Objective: {objective}\nOperation:", self._probe(plan)[0]["prompt"])

    def test_losslessness_check_rejects_invented_fragment(self):
        objective = "Update docs and run tests"
        plan = orchestrator.decompose(objective)
        nodes = copy.deepcopy(plan["nodes"])
        slicer = orchestrator._Slicer(orchestrator._clause_spans(objective))
        # A fabricated task with no source ownership cannot pass coverage validation.
        nodes.append({"task_id": "invented", "executor": "AI", "objective_summary": "invented"})
        self.assertFalse(orchestrator._assign_task_slices(nodes, slicer, objective))

    def test_whitespace_sensitive_identities_match_delivered_text(self):
        identities = []
        for literal in ('"alpha  beta"', '"alpha beta"'):
            objective = f"Fix output to exactly {literal}; update docs; run tests"
            plan = orchestrator.decompose(objective)
            node = plan["nodes"][0]
            request = self._probe(plan)[0]
            master = node["task_contract"]["master_fingerprint"]
            self.assertEqual(hashlib.sha256(objective.encode()).hexdigest()[:32], master)
            self.assertEqual(execution.delivered_input_fingerprint(
                node, [], request["context_manifest"]), request["delivered_input_fingerprint"])
            identities.append((master, result_cache.task_semantic_fingerprint(node),
                               request["delivered_input_fingerprint"]))
        for first, second in zip(*identities):
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
