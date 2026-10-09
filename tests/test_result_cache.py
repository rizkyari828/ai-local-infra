"""Result Cache + Incremental DAG Reuse V0.1 unit tests: identity, freshness,
eligibility, capability compatibility, corruption, secret safety, and the reuse
decision engine. Deterministic; no AI calls, no network."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import result_cache as rc  # noqa: E402


def _node(task_id="T1", *, executor="AI", family="configuration_resolution",
          task_class="NORMAL", objective="analyze the configuration", acceptance=("ok",),
          tier="BALANCED", effort="medium", depends=None):
    node = {"task_id": task_id, "executor": executor, "task_family": family,
            "task_class": task_class, "objective_summary": objective,
            "acceptance_criteria": list(acceptance), "depends_on": list(depends or []),
            "produces_artifacts": [], "consumes_results_from": []}
    if executor != "TOOL_ONLY":
        node["routing"] = {"tier": tier, "effort": effort}
    return node


def _item(kind, source_id, fingerprint, *, path=None, symbol=None, rng=None, required=True):
    return {"source_kind": kind, "source_id": source_id, "fingerprint": fingerprint,
            "path": path, "symbol": symbol, "range": rng, "required": required}


def _manifest(items, *, budget="SMALL", graph="MISSING"):
    return {"context_budget": budget, "graphify_status": graph, "items": list(items)}


def _success(summary="done", acceptance=None, decisions=None, risks=None):
    return {"status": "SUCCESS", "completion_signal": "agent_reported",
            "summary": summary, "acceptance_results": acceptance or {"ok": True},
            "decisions": decisions or [], "new_risk_signals": risks or [], "files_changed": []}


class IdentityTests(unittest.TestCase):
    def test_same_semantic_task_and_inputs_same_fingerprint(self) -> None:
        node = _node()
        m = _manifest([_item("OBJECTIVE_SUMMARY", "T1", "fp1")])
        dep = ["depfp1"]
        a = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=rc.task_semantic_fingerprint(node),
                                 context_fingerprint=rc.context_identity(m), dependency_fingerprints=dep,
                                 freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        b = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=rc.task_semantic_fingerprint(node),
                                 context_fingerprint=rc.context_identity(m), dependency_fingerprints=dep,
                                 freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        self.assertEqual(a, b)

    def test_explicit_semantic_key_proves_equivalence(self) -> None:
        a = _node(objective="audit the router precedence")
        b = _node(objective="inspect routing order resolution")
        self.assertNotEqual(rc.task_semantic_fingerprint(a), rc.task_semantic_fingerprint(b))
        self.assertEqual(rc.task_semantic_fingerprint(a, semantic_key="router-precedence"),
                         rc.task_semantic_fingerprint(b, semantic_key="router-precedence"))

    def test_semantic_task_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(rc.task_semantic_fingerprint(_node(objective="a")),
                            rc.task_semantic_fingerprint(_node(objective="b")))

    def test_source_change_changes_input_fingerprint(self) -> None:
        node = _node()
        semantic = rc.task_semantic_fingerprint(node)
        base = _manifest([_item("FILE_RANGE", "a.py", "fpv1", path="a.py", rng=[1, 10])])
        changed = _manifest([_item("FILE_RANGE", "a.py", "fpv2", path="a.py", rng=[1, 10])])
        a = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=semantic,
                                 context_fingerprint=rc.context_identity(base), dependency_fingerprints=[],
                                 freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        b = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=semantic,
                                 context_fingerprint=rc.context_identity(changed), dependency_fingerprints=[],
                                 freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        self.assertNotEqual(a, b)

    def test_dependency_fact_change_changes_input_fingerprint(self) -> None:
        node, semantic = _node(), None
        semantic = rc.task_semantic_fingerprint(node)
        a = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=semantic, context_fingerprint=None,
                                 dependency_fingerprints=["dep1"], freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        b = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=semantic, context_fingerprint=None,
                                 dependency_fingerprints=["dep2"], freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        self.assertNotEqual(a, b)

    def test_knowledge_dependency_change_changes_input_fingerprint(self) -> None:
        node = _node()
        semantic = rc.task_semantic_fingerprint(node)
        base = _manifest([_item("KNOWLEDGE", "lesson-x", "k1")])
        changed = _manifest([_item("KNOWLEDGE", "lesson-x", "k2")])
        self.assertNotEqual(rc.context_identity(base), rc.context_identity(changed))
        a = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=semantic,
                                 context_fingerprint=rc.context_identity(base), dependency_fingerprints=[],
                                 freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        b = rc.input_fingerprint(project_id="p", task_semantic_fingerprint=semantic,
                                 context_fingerprint=rc.context_identity(changed), dependency_fingerprints=[],
                                 freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        self.assertNotEqual(a, b)

    def test_unrelated_change_does_not_invalidate(self) -> None:
        # A task referencing only a.py is unaffected by a change to readme.md.
        base = _manifest([_item("FILE_RANGE", "a.py", "fp1", path="a.py", rng=[1, 5])])
        unrelated = _manifest([_item("FILE_RANGE", "a.py", "fp1", path="a.py", rng=[1, 5])])
        self.assertEqual(rc.context_identity(base), rc.context_identity(unrelated))

    def test_context_item_order_does_not_cause_false_miss(self) -> None:
        one = _manifest([_item("DEPENDENCY_FACT", "T0", "d1"), _item("FILE_RANGE", "a.py", "f1", path="a.py")])
        two = _manifest([_item("FILE_RANGE", "a.py", "f1", path="a.py"), _item("DEPENDENCY_FACT", "T0", "d1")])
        self.assertEqual(rc.context_identity(one), rc.context_identity(two))

    def test_acceptance_change_invalidates(self) -> None:
        self.assertNotEqual(rc.task_semantic_fingerprint(_node(acceptance=("ok",))),
                            rc.task_semantic_fingerprint(_node(acceptance=("ok", "also"))))


class FreshnessTests(unittest.TestCase):
    def test_ai_default_is_input_bound(self) -> None:
        self.assertEqual(rc.FRESHNESS_INPUT_BOUND, rc.freshness_policy(_node()))

    def test_volatile_cue_forces_runtime_volatile(self) -> None:
        self.assertEqual(rc.FRESHNESS_RUNTIME_VOLATILE,
                         rc.freshness_policy(_node(objective="check current provider health")))

    def test_tool_only_defaults_runtime_volatile(self) -> None:
        node = _node(executor="TOOL_ONLY")
        node["tool"] = {"operation": "repository_state"}
        self.assertEqual(rc.FRESHNESS_RUNTIME_VOLATILE, rc.freshness_policy(node))

    def test_explicit_override_and_constraints(self) -> None:
        node = _node()
        node["freshness_policy"] = rc.FRESHNESS_PURE
        self.assertEqual(rc.FRESHNESS_PURE, rc.freshness_policy(node))
        self.assertEqual(rc.FRESHNESS_ALWAYS_EXECUTE,
                         rc.freshness_policy(_node(), constraints={"freshness_policy": "ALWAYS_EXECUTE"}))


class EligibilityTests(unittest.TestCase):
    def test_failed_result_not_cached(self) -> None:
        ok, reason = rc.eligible_for_cache(_node(), {"status": "FAILURE"}, freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertFalse(ok)

    def test_partial_result_not_cached(self) -> None:
        ok, _ = rc.eligible_for_cache(_node(), {"status": "PARTIAL"}, freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertFalse(ok)

    def test_volatile_result_not_cached(self) -> None:
        ok, reason = rc.eligible_for_cache(_node(), _success(), freshness=rc.FRESHNESS_RUNTIME_VOLATILE)
        self.assertFalse(ok)
        self.assertEqual(rc.REASON_FRESHNESS_POLICY, reason)

    def test_unstructured_prose_not_reusable(self) -> None:
        ok, _ = rc.eligible_for_cache(_node(), {"status": "SUCCESS",
                                                "acceptance_results": {}}, freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertFalse(ok)
        self.assertIsNone(rc.bounded_result(_node(), {"status": "SUCCESS", "acceptance_results": {}}))

    def test_secret_bearing_result_rejected(self) -> None:
        result = _success(summary="token sk-" + "a" * 30, acceptance={"ok": True})
        ok, _ = rc.eligible_for_cache(_node(), result, freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertFalse(ok)

    def test_bounded_result_projection(self) -> None:
        bounded = rc.bounded_result(_node(), _success(summary="s", decisions=["d"], risks=["r"]))
        self.assertEqual({"summary", "decisions", "files_changed", "acceptance_results",
                          "new_risk_signals"}, set(bounded))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.store = rc.ResultCache(state_dir=self.state)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _write(self, **overrides):
        node = _node()
        bounded = rc.bounded_result(node, _success(summary=overrides.pop("summary", "done")))
        kwargs = dict(project_id="remote:x", task_semantic_fingerprint="sem1",
                      input_fingerprint="in1", context_manifest_fingerprint="ctx1",
                      result_fingerprint=rc.result_fingerprint("configuration_resolution", bounded),
                      result_type="configuration_resolution", result=bounded,
                      freshness_policy=rc.FRESHNESS_INPUT_BOUND, original_run_id="run1",
                      original_task_id="T1", original_backend="fake", original_provider="deepseek",
                      original_model="deepseek-flash", original_required_tier="BALANCED",
                      original_required_effort="medium", original_native_effort="medium",
                      original_normalized_effort="MEDIUM")
        kwargs.update(overrides)
        return self.store.write(**kwargs)

    def test_write_lookup_roundtrip_and_status(self) -> None:
        entry_id = self._write()
        entry = self.store.lookup("remote:x", "in1")
        self.assertEqual(entry_id, entry["cache_entry_id"])
        self.assertEqual("done", entry["result"]["summary"])
        self.assertEqual("fake", entry["original_backend"])
        self.assertEqual(1, self.store.status()["entries"])
        self.assertEqual(rc.SCHEMA_VERSION, self.store.status()["schema_version"])

    def test_corrupt_entry_ignored_safely(self) -> None:
        self._write()
        self.store.connection.execute("UPDATE cache_entries SET bounded_result='{not json'")
        self.store.connection.commit()
        self.assertTrue(rc.is_invalid(self.store.lookup("remote:x", "in1")))

    def test_tampered_result_fingerprint_is_invalid(self) -> None:
        self._write()
        self.store.connection.execute("UPDATE cache_entries SET result_fingerprint='deadbeef'")
        self.store.connection.commit()
        self.assertTrue(rc.is_invalid(self.store.lookup("remote:x", "in1")))

    def test_schema_mismatch_is_invalid(self) -> None:
        self._write()
        self.store.connection.execute("UPDATE cache_entries SET schema_version=999")
        self.store.connection.commit()
        self.assertTrue(rc.is_invalid(self.store.lookup("remote:x", "in1")))

    def test_secret_result_rejected_on_write(self) -> None:
        with self.assertRaises(rc.CacheError):
            self._write(result={"summary": "sk-" + "a" * 30}, result_type="x")

    def test_no_raw_prompt_or_response_columns(self) -> None:
        columns = {row[1] for row in self.store.connection.execute("PRAGMA table_info(cache_entries)")}
        for forbidden in ("prompt", "response", "transcript", "log", "token", "api_key"):
            self.assertNotIn(forbidden, columns)

    def test_refuses_cache_inside_repository(self) -> None:
        with self.assertRaises(rc.CacheError):
            rc.ResultCache(db=ROOT / "scripts" / "common" / "should-not-exist.db")


class CapabilityTests(unittest.TestCase):
    def _entry(self, tier, effort):
        return {"original_required_tier": tier, "original_required_effort": effort}

    def test_weaker_cached_result_rejected_for_stronger_task(self) -> None:
        result = rc.capability_compatible(self._entry("BALANCED", "medium"),
                                          required_tier="STRONG", required_effort="high")
        self.assertFalse(result["ok"])
        self.assertEqual(rc.REASON_WEAKER_CAPABILITY, result["reason"])

    def test_stronger_cached_result_may_satisfy_weaker_task(self) -> None:
        result = rc.capability_compatible(self._entry("STRONG", "high"),
                                          required_tier="BALANCED", required_effort="medium")
        self.assertTrue(result["ok"])

    def test_stronger_rejected_when_policy_disallows(self) -> None:
        result = rc.capability_compatible(self._entry("STRONG", "high"), required_tier="BALANCED",
                                          required_effort="medium", allow_stronger=False)
        self.assertFalse(result["ok"])

    def test_equal_tier_weaker_effort_rejected(self) -> None:
        result = rc.capability_compatible(self._entry("STRONG", "medium"), required_tier="STRONG",
                                          required_effort="high")
        self.assertFalse(result["ok"])


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.store = rc.ResultCache(state_dir=self.state)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _identity(self, node, *, context="ctx1", deps=None, freshness=rc.FRESHNESS_INPUT_BOUND):
        semantic = rc.task_semantic_fingerprint(node)
        fp = rc.input_fingerprint(project_id="remote:x", task_semantic_fingerprint=semantic,
                                  context_fingerprint=context, dependency_fingerprints=deps or [],
                                  freshness_policy=freshness)
        return {"task_semantic_fingerprint": semantic, "context_manifest_fingerprint": context,
                "input_fingerprint": fp}

    def _seed(self, node, identity, tier="BALANCED", effort="medium"):
        bounded = rc.bounded_result(node, _success())
        return self.store.write(project_id="remote:x",
                                task_semantic_fingerprint=identity["task_semantic_fingerprint"],
                                input_fingerprint=identity["input_fingerprint"],
                                context_manifest_fingerprint=identity["context_manifest_fingerprint"],
                                result_fingerprint=rc.result_fingerprint(rc.result_type(node), bounded),
                                result_type=rc.result_type(node), result=bounded,
                                freshness_policy=rc.FRESHNESS_INPUT_BOUND,
                                original_required_tier=tier, original_required_effort=effort,
                                original_task_id=node["task_id"])

    def test_miss_then_hit(self) -> None:
        node = _node()
        identity = self._identity(node)
        miss = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                         required_tier="BALANCED", required_effort="medium",
                         freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertEqual(rc.RERUN, miss["decision"])
        self.assertEqual(rc.REASON_CACHE_MISS, miss["reason"])
        self._seed(node, identity)
        hit = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                        required_tier="BALANCED", required_effort="medium",
                        freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertEqual(rc.REUSE, hit["decision"])
        self.assertEqual(rc.REASON_UNCHANGED_INPUTS, hit["reason"])

    def test_force_refresh_and_explicit_model_bypass(self) -> None:
        node = _node()
        identity = self._identity(node)
        self._seed(node, identity)
        refreshed = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                             required_tier="BALANCED", required_effort="medium",
                             freshness=rc.FRESHNESS_INPUT_BOUND, force_refresh=True)
        self.assertEqual(rc.REASON_USER_FORCED_REFRESH, refreshed["reason"])
        explicit = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                            required_tier="BALANCED", required_effort="medium",
                            freshness=rc.FRESHNESS_INPUT_BOUND, explicit_model=True)
        self.assertEqual(rc.REASON_EXPLICIT_MODEL, explicit["reason"])

    def test_incomplete_dependencies_force_rerun(self) -> None:
        node = _node()
        identity = self._identity(node)
        self._seed(node, identity)
        decision = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                            required_tier="BALANCED", required_effort="medium",
                            freshness=rc.FRESHNESS_INPUT_BOUND, dependencies_complete=False)
        self.assertEqual(rc.REASON_DEPENDENCY_CHANGED, decision["reason"])

    def test_volatile_forces_rerun(self) -> None:
        node = _node()
        identity = self._identity(node)
        decision = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                            required_tier="BALANCED", required_effort="medium",
                            freshness=rc.FRESHNESS_RUNTIME_VOLATILE)
        self.assertEqual(rc.REASON_FRESHNESS_POLICY, decision["reason"])

    def test_wrong_repository_identity_rejected(self) -> None:
        node = _node()
        identity = self._identity(node)
        self._seed(node, identity)
        # A lookup under a different project cannot see remote:x's entry.
        decision = rc.decide(store=self.store, project_id="remote:other", node=node, identity=identity,
                            required_tier="BALANCED", required_effort="medium",
                            freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertEqual(rc.REASON_CACHE_MISS, decision["reason"])

    def test_weaker_cached_capability_rejected(self) -> None:
        node = _node()
        identity = self._identity(node)
        self._seed(node, identity, tier="BALANCED", effort="medium")
        decision = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                            required_tier="STRONG", required_effort="high",
                            freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertEqual(rc.REASON_WEAKER_CAPABILITY, decision["reason"])

    def test_tool_only_volatile_reruns(self) -> None:
        node = _node(executor="TOOL_ONLY")
        node["tool"] = {"operation": "repository_state"}
        identity = self._identity(node)
        decision = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                            required_tier=None, required_effort=None,
                            freshness=rc.freshness_policy(node))
        self.assertEqual(rc.REASON_FRESHNESS_POLICY, decision["reason"])

    def test_pure_tool_reuses_only_with_entry(self) -> None:
        node = _node(executor="TOOL_ONLY")
        node["tool"] = {"operation": "config_parse"}
        node["freshness_policy"] = rc.FRESHNESS_PURE
        freshness = rc.freshness_policy(node)
        identity = self._identity(node, freshness=freshness)
        miss = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                        required_tier=None, required_effort=None, freshness=freshness)
        self.assertEqual(rc.REASON_CACHE_MISS, miss["reason"])
        bounded = rc.bounded_result(node, {"status": "SUCCESS", "result": {"parsed": True}})
        self.store.write(project_id="remote:x", task_semantic_fingerprint=identity["task_semantic_fingerprint"],
                         input_fingerprint=identity["input_fingerprint"],
                         context_manifest_fingerprint=identity["context_manifest_fingerprint"],
                         result_fingerprint=rc.result_fingerprint(rc.result_type(node), bounded),
                         result_type=rc.result_type(node), result=bounded,
                         freshness_policy=rc.FRESHNESS_PURE, original_task_id="T1")
        hit = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                       required_tier=None, required_effort=None, freshness=freshness)
        self.assertEqual(rc.REUSE, hit["decision"])
        # A tool entry never carries provider/model provenance.
        entry = self.store.lookup("remote:x", identity["input_fingerprint"])
        self.assertIsNone(entry["original_provider"])
        self.assertIsNone(entry["original_model"])


if __name__ == "__main__":
    unittest.main()
