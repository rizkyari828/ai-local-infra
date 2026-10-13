"""Execution Contract Stabilization V0.1 — FIX PASS 5 lossless decomposition.

Deterministic, fake/stub adapters only, temporary repositories, synthetic fixtures.
Zero real model/provider calls. Guards the remaining ASTRA P3 invariant:

    AUTHORITATIVE EXECUTABLE TEXT MUST NEVER BE REWRITTEN BY DECOMPOSITION.

Commas, semicolons and standalone "and" inside quoted/backtick/code literals are
literal content, never clause boundaries. When lossless splitting cannot be proven the
original objective is kept intact rather than reconstructed.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import execution  # noqa: E402
import orchestrator  # noqa: E402


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "input.txt").write_text("input v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


def _ai_nodes(plan: dict) -> list[dict]:
    return [node for node in plan["nodes"] if node.get("executor") == "AI"]


def _objectives(plan: dict) -> list[str]:
    return [node["task_contract"]["objective"] for node in _ai_nodes(plan)]


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, plan, backend=None, **kw):
        kw.setdefault("confirm", True)
        kw.setdefault("allow_dirty", True)
        kw.setdefault("record_evidence", False)
        backend = backend or execution.FakeBackend()
        return execution.execute_plan(plan, backend=backend, state_dir=self.state,
                                      repo_root=self.root, **kw), backend

    def _probe(self, plan) -> list[dict]:
        captured: list[dict] = []

        class Probe(execution.FakeBackend):
            def execute(self, request):
                captured.append(request)
                return super().execute(request)

        self._run(plan, backend=Probe())
        self.assertTrue(captured)
        return captured


# --------------------------------------------------------------------------- #
# Required regressions 1-7: literal separators must never become boundaries
# --------------------------------------------------------------------------- #
class LiteralPreservationTests(Fixture):
    def test_double_quoted_and_is_not_a_separator(self) -> None:
        # 1. Input with quoted "and" and a doubled space must survive verbatim.
        exact = 'Fix output to exactly "alpha  and  beta"; update docs; run tests.'
        plan = orchestrator.decompose(exact)
        objectives = _objectives(plan)
        self.assertIn('Fix output to exactly "alpha  and  beta"', objectives)
        blob = " ".join(objectives)
        self.assertNotIn("alpha; beta", blob)
        self.assertNotIn('"alpha; beta"', blob)

    def test_double_quoted_comma_is_preserved(self) -> None:
        # 2. Literal comma preserved exactly.
        exact = 'Fix output to exactly "alpha,beta"; update docs; run tests.'
        self.assertIn('Fix output to exactly "alpha,beta"', _objectives(orchestrator.decompose(exact)))

    def test_double_quoted_semicolon_is_preserved(self) -> None:
        # 3. Literal semicolon preserved exactly.
        exact = 'Fix output to exactly "alpha;beta"; update docs; run tests.'
        self.assertIn('Fix output to exactly "alpha;beta"', _objectives(orchestrator.decompose(exact)))

    def test_single_quoted_text_is_preserved(self) -> None:
        # 4. Single-quoted variant.
        exact = "Fix output to exactly 'alpha and beta'; update docs; run tests."
        self.assertIn("Fix output to exactly 'alpha and beta'", _objectives(orchestrator.decompose(exact)))

    def test_backtick_code_literal_is_preserved(self) -> None:
        # 5. Backtick/code-literal variant.
        exact = "Fix output to exactly `alpha && beta`; update docs; run tests."
        self.assertIn("Fix output to exactly `alpha && beta`", _objectives(orchestrator.decompose(exact)))

    def test_multiple_separators_inside_one_literal_are_preserved(self) -> None:
        # 6. Several separators inside one literal stay literal.
        exact = 'Fix output to exactly "alpha, beta; gamma and delta"; update docs; run tests.'
        self.assertIn('Fix output to exactly "alpha, beta; gamma and delta"',
                      _objectives(orchestrator.decompose(exact)))

    def test_whitespace_sensitive_literal_keeps_distinct_identity(self) -> None:
        # 7. "alpha  beta" must remain distinct from "alpha beta" in both master and
        #    delivered task identity, and both survive the decomposition verbatim.
        exact = 'Fix output to exactly "alpha  beta"; update docs; run tests.'
        collapsed = 'Fix output to exactly "alpha beta"; update docs; run tests.'
        left, right = orchestrator.decompose(exact), orchestrator.decompose(collapsed)
        left_node = next(n for n in _ai_nodes(left) if '"alpha' in n["task_contract"]["objective"])
        right_node = next(n for n in _ai_nodes(right) if '"alpha' in n["task_contract"]["objective"])
        self.assertIn('"alpha  beta"', left_node["task_contract"]["objective"])
        self.assertNotEqual(left_node["task_contract"]["master_fingerprint"],
                            right_node["task_contract"]["master_fingerprint"])
        self.assertNotEqual(execution.delivered_input_fingerprint(left_node, [], None),
                            execution.delivered_input_fingerprint(right_node, [], None))


# --------------------------------------------------------------------------- #
# Required regressions 8-9: safe decomposition still works, ambiguity falls back
# --------------------------------------------------------------------------- #
class DecompositionBoundaryTests(Fixture):
    def test_plain_objective_may_still_decompose(self) -> None:
        # 8. No protected literal -> proven-safe split is still allowed.
        plan = orchestrator.decompose("Update docs and run tests")
        self.assertGreater(len(plan["nodes"]), 1)
        blob = " ".join(_objectives(plan)).lower()
        self.assertIn("docs", blob)
        self.assertIn("test", blob)

    def test_ambiguous_literal_objective_falls_back_intact(self) -> None:
        # 9. A leftover clause next to a protected literal can only be represented by
        #    inventing a joined string, so the whole objective is kept intact.
        exact = 'Fix output to "alpha,beta" and preserve legacy behavior; update docs'
        plan = orchestrator.decompose(exact)
        nodes = _ai_nodes(plan)
        self.assertEqual(1, len(nodes))
        self.assertEqual(exact, nodes[0]["task_contract"]["objective"])

    def test_unterminated_literal_falls_back_intact(self) -> None:
        exact = 'Fix output to exactly "alpha and beta; update docs'
        self.assertIsNone(orchestrator._clauses(exact))
        nodes = _ai_nodes(orchestrator.decompose(exact))
        self.assertEqual(1, len(nodes))
        self.assertEqual(exact, nodes[0]["task_contract"]["objective"])


# --------------------------------------------------------------------------- #
# Required regressions 10-11: authoritative text reaches the worker and identity
# --------------------------------------------------------------------------- #
class WorkerAuthoritativeTextTests(Fixture):
    def test_worker_prompt_contains_authoritative_literal_exactly(self) -> None:
        # 10. The worker must receive the exact literal, never a normalized rewrite.
        exact = 'Fix output to exactly "alpha  and  beta"; update docs; run tests.'
        prompts = [request["prompt"] for request in self._probe(orchestrator.decompose(exact))]
        self.assertTrue(any('"alpha  and  beta"' in prompt for prompt in prompts))
        for prompt in prompts:
            self.assertNotIn("alpha; beta", prompt)

    def test_delivered_identity_matches_authoritative_semantics(self) -> None:
        # 11. Same authoritative objective -> same delivered identity; a rewritten
        #     objective would not collapse into it.
        exact = 'Fix output to exactly "alpha  and  beta"; update docs; run tests.'
        same = 'Fix output to exactly "alpha  and  beta"; update docs; run tests.'
        rewritten = 'Fix output to exactly "alpha; beta"; update docs; run tests.'

        def node(plan):
            return next(n for n in _ai_nodes(plan) if '"alpha' in n["task_contract"]["objective"])

        first = node(orchestrator.decompose(exact))
        second = node(orchestrator.decompose(same))
        third = node(orchestrator.decompose(rewritten))
        self.assertEqual(execution.delivered_input_fingerprint(first, [], None),
                         execution.delivered_input_fingerprint(second, [], None))
        self.assertNotEqual(execution.delivered_input_fingerprint(first, [], None),
                            execution.delivered_input_fingerprint(third, [], None))


# --------------------------------------------------------------------------- #
# Independent adversarial probes Q1-Q7
# --------------------------------------------------------------------------- #
class AdversarialProbes(Fixture):
    def _literal_survives(self, literal: str) -> None:
        exact = f'Fix output to exactly {literal}; update docs; run tests.'
        objectives = _objectives(orchestrator.decompose(exact))
        self.assertIn(f"Fix output to exactly {literal}", objectives)

    def test_q1_quoted_and(self) -> None:
        self._literal_survives('"x and y"')

    def test_q2_quoted_comma(self) -> None:
        self._literal_survives('"x,y"')

    def test_q3_quoted_semicolon(self) -> None:
        self._literal_survives('"x;y"')

    def test_q4_single_quotes(self) -> None:
        self._literal_survives("'x and y'")

    def test_q5_backticks(self) -> None:
        self._literal_survives("`x and y; z`")

    def test_q6_multiple_separators_inside_one_literal(self) -> None:
        self._literal_survives('"x, y; z and w"')

    def test_q7_plain_compound_objective(self) -> None:
        plan = orchestrator.decompose("Update docs, run tests and report the final result")
        self.assertGreater(len(plan["nodes"]), 1)
        self.assertTrue(plan["valid"])


if __name__ == "__main__":
    unittest.main()
