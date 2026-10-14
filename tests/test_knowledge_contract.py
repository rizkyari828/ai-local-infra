"""Shared Knowledge Contract V0.1 — deterministic offline fixtures and rules.

Every fixture here is data: no file read, no Git call, no provider call, no clock.
The F-numbers match the required fixture list of the contract workflow; the
accepted/current identity fixture is marked ``REUSE``.

The contract owns the *protocol* (ownership, requiredness, authority, identity,
refusal). It never grants runtime permission, and it changes no production
cache/resume behaviour — both are asserted, not assumed.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import authority  # noqa: E402
import knowledge_contract as kc  # noqa: E402

PROJECT_REPO = "remote:https://example.com/org/project.git"
INFRA_REPO = "remote:https://example.com/org/ai-local-infra.git"
ARCH = {"key": "project.architecture.overview", "owner": "project", "kind": "architecture",
        "locator": "docs/ARCHITECTURE.md", "authority": "AUTHORITATIVE", "review": "REVIEWED",
        "lifecycle": "ACTIVE", "required_semantics": ["api_contract"], "depends_on": []}
API = {"key": "project.api.contract", "owner": "project", "kind": "api", "locator": "docs/api.md",
       "authority": "SUPPORTING", "review": "REVIEWED", "lifecycle": "ACTIVE"}
CONTEXT = {"task_family": "architecture_review", "task_class": "CORRECTNESS_SENSITIVE",
           "language": "python", "surface": "codex-cli"}


def source(**overrides) -> dict:
    record = dict(ARCH)
    record.update(overrides)
    return record


def requirement(**overrides) -> dict:
    record = {"key": ARCH["key"], "required": True, "scope": "task_family:architecture_review",
              "reason": "task_family match", "min_authority": "AUTHORITATIVE"}
    record.update(overrides)
    return record


def document(*, sources=None, requirements=None, contents=None, context=None,
             repositories=None, **overrides) -> dict:
    """A valid contract document; every fixture below is one deterministic patch of it."""
    doc = {
        "contract_version": kc.CONTRACT_VERSION,
        "repositories": {"project": PROJECT_REPO, "ai_local_infra": INFRA_REPO},
        "sources": [source()] if sources is None else sources,
        "requirements": [requirement()] if requirements is None else requirements,
        "contents": {"project.architecture.overview": {"content": "architecture v1\n"}}
        if contents is None else contents,
        "scope": "task_family:architecture_review",
        "reason": "declared knowledge requirement",
        "context": dict(CONTEXT) if context is None else context,
    }
    if repositories is not None:
        doc["repositories"] = repositories
    doc.update(overrides)
    return doc


def select(**overrides) -> dict:
    return kc.select(document(**overrides))


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path: pathlib.Path, remote: str | None = None) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    (path / "docs").mkdir(exist_ok=True)
    (path / "docs" / "ARCHITECTURE.md").write_text("architecture\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return path


class ProtocolTests(unittest.TestCase):
    def test_contract_reports_its_versions_and_shape(self) -> None:
        result = select()
        self.assertEqual(kc.CONTRACT_VERSION, result["contract_version"])
        self.assertEqual(kc.SELECTOR_VERSION, result["selector_version"])
        self.assertEqual(kc.VALIDATOR_VERSION, result["validator_version"])
        self.assertEqual(sorted(kc.CONTRACT_DOCUMENT_KEYS), sorted(result))

    def test_selection_is_deterministic(self) -> None:
        first, second = select(), select()
        self.assertEqual(first["manifest_fingerprint"], second["manifest_fingerprint"])
        self.assertEqual(first["delivered_identity"], second["delivered_identity"])
        self.assertEqual(first["delivered_content_fingerprint"],
                         second["delivered_content_fingerprint"])

    def test_unsupported_contract_version_is_refused(self) -> None:
        result = kc.select({"contract_version": "shared-knowledge-contract-v99"})
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"])
        self.assertEqual(kc.UNSUPPORTED_CONTRACT_VERSION, result["refusals"][0]["reason"])
        self.assertEqual([], result["entries"])

    def test_entries_and_omissions_are_deterministically_ordered(self) -> None:
        spec = dict(sources=[source(), dict(API)],
                    requirements=[requirement(),
                                  requirement(key=API["key"], min_authority="SUPPORTING")],
                    contents={"project.architecture.overview": {"content": "a\n"},
                              "project.api.contract": {"content": "b\n"}})
        result = kc.select(document(**spec))
        self.assertEqual(sorted([ARCH["key"], API["key"]]),
                         [entry["key"] for entry in result["entries"]])
        self.assertEqual(result["delivered_identity"], kc.select(document(**spec))["delivered_identity"])
        omitted = kc.select(document(**{**spec, "requirements": [requirement(), requirement(
            key=API["key"], required=False, min_authority="AUTHORITATIVE")]}))
        self.assertEqual([API["key"]], [record["key"] for record in omitted["omissions"]])

    def test_contract_performs_no_io_and_no_runtime_binding(self) -> None:
        # Scope discipline: a pure protocol, not a shared-knowledge runtime.
        text = (ROOT / "scripts/common/knowledge_contract.py").read_text(encoding="utf-8")
        for forbidden in ("import subprocess", "import urllib", "import socket", "import sqlite3",
                          "import providers", "import execution", "import authority",
                          "import context_retrieval"):
            self.assertNotIn(forbidden, text, f"the contract must stay pure: {forbidden}")
        for master_b in ("claim", "lease", "heartbeat"):
            self.assertNotIn(f"def {master_b}", text)

    def test_no_production_module_consumes_the_contract_yet(self) -> None:
        # V0.1 is a contract: production cache/resume/execution behaviour is unchanged.
        for module in ("execution.py", "lifecycle.py", "result_cache.py", "orchestrate_cli.py",
                       "context_retrieval.py", "orchestration_store.py"):
            text = (ROOT / "scripts/common" / module).read_text(encoding="utf-8")
            self.assertNotIn("knowledge_contract", text, module)


class ContentIdentityTests(unittest.TestCase):
    def test_F1_selected_content_change_changes_delivered_identity(self) -> None:
        before = select()
        after = select(contents={"project.architecture.overview": {"content": "architecture v2\n"}})
        self.assertNotEqual(before["delivered_identity"], after["delivered_identity"])
        self.assertNotEqual(before["manifest_fingerprint"], after["manifest_fingerprint"])
        self.assertNotEqual(before["entries"][0]["content_fingerprint"],
                            after["entries"][0]["content_fingerprint"])
        self.assertEqual(before["entries"][0]["source_identity"], after["entries"][0]["source_identity"])

    def test_F2_unselected_unrelated_change_does_not_change_identity(self) -> None:
        extra = {"key": "project.unrelated.note", "owner": "project", "kind": "note",
                 "locator": "notes/other.md", "authority": "OBSERVED", "review": "UNREVIEWED",
                 "lifecycle": "ACTIVE"}
        base = select()
        with_extra = select(sources=[source(), extra],
                            contents={"project.architecture.overview": {"content": "architecture v1\n"},
                                      "project.unrelated.note": {"content": "one\n"}})
        changed = select(sources=[source(), extra],
                         contents={"project.architecture.overview": {"content": "architecture v1\n"},
                                   "project.unrelated.note": {"content": "two\n"}})
        # An unselected source is not hashed: no whole-repository fingerprinting.
        self.assertEqual(base["delivered_identity"], with_extra["delivered_identity"])
        self.assertEqual(with_extra["delivered_identity"], changed["delivered_identity"])

    def test_F9_path_spelling_alone_does_not_change_identity(self) -> None:
        base = select()
        for spelling in ("./docs/ARCHITECTURE.md", "docs//ARCHITECTURE.md", "docs/./ARCHITECTURE.md"):
            self.assertEqual(base["delivered_identity"], select(sources=[source(locator=spelling)])["delivered_identity"],
                             spelling)
        self.assertEqual("docs/ARCHITECTURE.md", kc.normalize_locator("./docs//./ARCHITECTURE.md"))

    def test_locator_must_be_project_relative(self) -> None:
        for locator in ("/etc/passwd", "../secrets.md", "docs/../../etc/passwd", "C:/Windows/x", ""):
            result = select(sources=[source(locator=locator)])
            self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"], locator)
            self.assertEqual(kc.INVALID, result["refusals"][0]["reason"], locator)

    def test_F10_same_relative_locator_in_an_unrelated_repository_is_a_different_source(self) -> None:
        other = select(repositories={"project": "remote:https://example.com/org/OTHER.git",
                                     "ai_local_infra": INFRA_REPO})
        self.assertNotEqual(select()["entries"][0]["source_identity"],
                            other["entries"][0]["source_identity"])
        self.assertNotEqual(select()["delivered_identity"], other["delivered_identity"])

    def test_F10_with_canonical_repository_identity_from_temp_repositories(self) -> None:
        # The integration binding: repository identity comes from authority, never invented here.
        with tempfile.TemporaryDirectory() as tmp:
            first = _repo(pathlib.Path(tmp) / "A" / "project")
            second = _repo(pathlib.Path(tmp) / "B" / "project")
            identity_a = authority.repository_identity(first)["id"]
            identity_b = authority.repository_identity(second)["id"]
            self.assertNotEqual(identity_a, identity_b)
            for identity in (identity_a, identity_b):
                self.assertEqual(identity, authority.repository_identity(
                    pathlib.Path(tmp) / ("A" if identity == identity_a else "B") / "project")["id"])
            left = select(repositories={"project": identity_a, "ai_local_infra": INFRA_REPO})
            right = select(repositories={"project": identity_b, "ai_local_infra": INFRA_REPO})
            self.assertNotEqual(left["entries"][0]["source_identity"],
                                right["entries"][0]["source_identity"])
            self.assertNotEqual(left["delivered_identity"], right["delivered_identity"])

    def test_declared_revision_revalidates_and_unrelated_commit_does_not(self) -> None:
        self.assertEqual(select()["delivered_identity"],
                         select()["delivered_identity"])  # no revision declared: HEAD is irrelevant
        pinned = select(sources=[source(revision="sha-a")])
        moved = select(sources=[source(revision="sha-b")])
        self.assertNotEqual(pinned["delivered_identity"], moved["delivered_identity"])

    def test_dependency_fingerprints_invalidate_the_dependent_entry(self) -> None:
        spec = dict(sources=[dict(API), source(depends_on=[API["key"]])],
                    requirements=[requirement(),
                                  requirement(key=API["key"], min_authority="SUPPORTING")],
                    contents={"project.architecture.overview": {"content": "a\n"},
                              "project.api.contract": {"content": "api v1\n"}})
        first = kc.select(document(**spec))
        second = kc.select(document(**{**spec,
                                       "contents": {"project.architecture.overview": {"content": "a\n"},
                                                    "project.api.contract": {"content": "api v2\n"}}}))
        arch_first = next(e for e in first["entries"] if e["key"] == ARCH["key"])
        arch_second = next(e for e in second["entries"] if e["key"] == ARCH["key"])
        self.assertEqual(1, len(arch_first["dependency_fingerprints"]))
        self.assertNotEqual(arch_first["entry_fingerprint"], arch_second["entry_fingerprint"])
        self.assertNotEqual(first["delivered_identity"], second["delivered_identity"])

    def test_unavailable_dependency_is_explicit(self) -> None:
        result = select(sources=[source(depends_on=[API["key"]])])
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"])
        self.assertEqual(kc.DEPENDENCY_UNAVAILABLE, result["refusals"][0]["reason"])


class RequirednessAndRefusalTests(unittest.TestCase):
    def test_F3_missing_required_source_refuses(self) -> None:
        result = select(sources=[])
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"])
        self.assertEqual(kc.MISSING, result["refusals"][0]["reason"])
        self.assertEqual([], result["entries"])
        self.assertEqual([], result["omissions"])

    def test_F4_missing_optional_source_is_a_deterministic_omission(self) -> None:
        result = select(sources=[], requirements=[requirement(required=False)])
        self.assertEqual(kc.READY, result["outcome"])
        self.assertEqual([], result["refusals"])
        self.assertEqual(kc.MISSING, result["omissions"][0]["reason"])
        self.assertFalse(result["omissions"][0]["required"])
        self.assertEqual(result["delivered_identity"], select(sources=[], requirements=[
            requirement(required=False)])["delivered_identity"])

    def test_missing_content_for_a_selected_source(self) -> None:
        required = select(contents={})
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, required["outcome"])
        self.assertEqual(kc.MISSING_CONTENT, required["refusals"][0]["reason"])
        optional = select(contents={}, requirements=[requirement(required=False)])
        self.assertEqual(kc.READY, optional["outcome"])
        self.assertEqual(kc.MISSING_CONTENT, optional["omissions"][0]["reason"])

    def test_F5_optional_to_required_changes_validation_and_identity(self) -> None:
        weakened = [source(authority="SUPPORTING")]  # below the AUTHORITATIVE requirement
        as_optional = select(sources=weakened, requirements=[requirement(required=False)])
        as_required = select(sources=weakened, requirements=[requirement(required=True)])
        self.assertEqual(kc.READY, as_optional["outcome"])
        self.assertEqual(kc.AUTHORITY_INCOMPATIBLE, as_optional["omissions"][0]["reason"])
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, as_required["outcome"])
        self.assertNotEqual(as_optional["delivered_identity"], as_required["delivered_identity"])
        # Requiredness is part of a selected entry's identity too.
        self.assertNotEqual(select()["entries"][0]["entry_fingerprint"],
                            select(requirements=[requirement(required=False)])["entries"][0]["entry_fingerprint"])

    def test_F6_supersession_and_revocation_reject_stale_authority(self) -> None:
        superseded = select(sources=[source(lifecycle="SUPERSEDED",
                                            superseded_by="project.architecture.v2")])
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, superseded["outcome"])
        self.assertEqual(kc.SUPERSEDED, superseded["refusals"][0]["reason"])
        self.assertIn("project.architecture.v2", superseded["refusals"][0]["detail"])
        self.assertEqual([], superseded["entries"])
        revoked = select(sources=[source(lifecycle="REVOKED")])
        self.assertEqual(kc.REVOKED, revoked["refusals"][0]["reason"])
        # The stale state is also visible in identity: a state change revalidates, and the
        # superseding source is a different source identity.
        self.assertNotEqual(select()["delivered_identity"], revoked["delivered_identity"])
        self.assertNotEqual(select()["delivered_identity"], superseded["delivered_identity"])
        replacement = select(sources=[source(key="project.architecture.v2", locator="docs/ARCH-v2.md")],
                            requirements=[requirement(key="project.architecture.v2")],
                            contents={"project.architecture.v2": {"content": "architecture v2\n"}})
        self.assertEqual(kc.READY, replacement["outcome"])
        self.assertNotEqual(select()["entries"][0]["source_identity"],
                            replacement["entries"][0]["source_identity"])

    def test_revoked_authority_that_is_optional_is_omitted(self) -> None:
        result = select(sources=[source(lifecycle="REVOKED")], requirements=[requirement(required=False)])
        self.assertEqual(kc.READY, result["outcome"])
        self.assertEqual(kc.REVOKED, result["omissions"][0]["reason"])

    def test_F7_review_state_change_revalidates_deterministically(self) -> None:
        reviewed = select()
        unreviewed = select(sources=[source(review="UNREVIEWED")])
        self.assertEqual(kc.READY, reviewed["outcome"])
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, unreviewed["outcome"])
        self.assertEqual(kc.UNREVIEWED, unreviewed["refusals"][0]["reason"])
        self.assertNotEqual(reviewed["delivered_identity"], unreviewed["delivered_identity"])
        # An undeclared review state is never implied to be REVIEWED.
        undeclared = select(sources=[{k: v for k, v in source().items() if k != "review"}])
        self.assertEqual(kc.UNREVIEWED, undeclared["refusals"][0]["reason"])

    def test_F7_applicability_change_revalidates_at_both_levels(self) -> None:
        applicable = select(sources=[source(applicability={"task_families": ["architecture_review"]})])
        self.assertEqual(kc.READY, applicable["outcome"])
        source_scope = select(sources=[source(applicability={"task_families": ["architecture_review"]})],
                              context={"task_family": "code_review"})
        self.assertEqual(kc.READY, source_scope["outcome"])
        self.assertEqual(kc.NOT_APPLICABLE, source_scope["omissions"][0]["reason"])
        self.assertEqual("source", source_scope["omissions"][0]["level"])
        requirement_scope = select(requirements=[requirement(applicability={"task_classes": ["NORMAL"]})])
        self.assertEqual(kc.READY, requirement_scope["outcome"])
        self.assertEqual("requirement", requirement_scope["omissions"][0]["level"])
        for variant in (source_scope, requirement_scope):
            self.assertNotEqual(applicable["delivered_identity"], variant["delivered_identity"])

    def test_F8_redaction_that_removes_required_semantics_refuses(self) -> None:
        result = select(contents={"project.architecture.overview": {
            "content": "architecture with api contract\n", "delivered": "architecture\n",
            "redactions": ["api_contract"]}})
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"])
        self.assertEqual(kc.REDACTION_INSUFFICIENT, result["refusals"][0]["reason"])
        optional = select(contents={"project.architecture.overview": {
            "content": "x\n", "delivered": "x\n", "redactions": ["api_contract"]}},
            requirements=[requirement(required=False)])
        self.assertEqual(kc.READY, optional["outcome"])
        self.assertEqual(kc.REDACTION_INSUFFICIENT, optional["omissions"][0]["reason"])

    def test_redaction_without_required_semantics_is_recorded_and_changes_delivery(self) -> None:
        result = select(contents={"project.architecture.overview": {
            "content": "architecture v1 secret\n", "delivered": "architecture v1\n",
            "redactions": ["credential"]}})
        self.assertEqual(kc.READY, result["outcome"])
        entry = result["entries"][0]
        self.assertNotEqual(entry["content_fingerprint"], entry["delivered_content_fingerprint"])
        self.assertNotEqual(select()["delivered_identity"], result["delivered_identity"])

    def test_malformed_document_refuses_instead_of_selecting(self) -> None:
        cases = [
            ([source(extra_field=1)], kc.UNKNOWN_FIELD),
            ([source(locator="/absolute")], kc.INVALID),
        ]
        for sources, reason in cases:
            with self.subTest(reason=reason):
                result = select(sources=sources)
                self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"])
                self.assertEqual(reason, result["refusals"][0]["reason"])
        duplicate = select(requirements=[requirement(), requirement(required=False)])
        self.assertEqual(kc.DUPLICATE_KEY, duplicate["refusals"][0]["reason"])
        bad_repo = select(repositories={"nope": PROJECT_REPO})
        self.assertEqual(kc.INVALID_DOCUMENT, bad_repo["refusals"][0]["reason"])
        empty_contents = select(contents="not-an-object")
        self.assertEqual(kc.INVALID_DOCUMENT, empty_contents["refusals"][0]["reason"])

    def test_source_kind_authority_and_lifecycle_vocabulary_is_validated(self) -> None:
        for overrides in ({"kind": "unknown"}, {"authority": "MIGHTY"}, {"lifecycle": "OLD"},
                          {"lifecycle": "SUPERSEDED"}):
            result = select(sources=[source(**overrides)])
            self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"], overrides)
            self.assertEqual(kc.INVALID, result["refusals"][0]["reason"], overrides)


class PermissionBoundaryTests(unittest.TestCase):
    """F13 — knowledge can never grant runtime permission."""

    def test_every_permission_field_is_rejected_by_name(self) -> None:
        for field in kc.PERMISSION_GRANT_FIELDS:
            with self.subTest(field=field):
                result = select(sources=[source(**{field: True})])
                self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"])
                self.assertEqual(kc.PERMISSION_GRANT_REJECTED, result["refusals"][0]["reason"])
                self.assertIn(field, result["refusals"][0]["detail"])
                requirement_attempt = select(requirements=[requirement(**{field: True})])
                self.assertEqual(kc.PERMISSION_GRANT_REJECTED,
                                 requirement_attempt["refusals"][0]["reason"])
                content_attempt = select(contents={"project.architecture.overview": {
                    "content": "x", field: True}})
                self.assertEqual(kc.PERMISSION_GRANT_REJECTED,
                                 content_attempt["refusals"][0]["reason"])

    def test_knowledge_cannot_change_machine_or_provider_permission(self) -> None:
        import os
        import unittest.mock

        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mac"}):
            before = authority.machine_policy(dict(os.environ))
            before_permission = authority.provider_permission("ollama")
            attempt = select(sources=[source(provider="ollama", local_only=True,
                                             workspace="/tmp", tool="shell.exec")])
            self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, attempt["outcome"])
            after = authority.machine_policy(dict(os.environ))
            self.assertEqual(before, after)
            self.assertEqual(before_permission, authority.provider_permission("ollama"))
            self.assertFalse(authority.provider_permission("ollama")["allowed"])
            # And nothing the contract delivers carries a permission-bearing key.
            granted = kc.select(document())
            self.assertFalse(set(kc.PERMISSION_GRANT_FIELDS) & set(granted))
            for entry in granted["entries"]:
                self.assertFalse(set(kc.PERMISSION_GRANT_FIELDS) & set(entry))


class ActiveWorkContractTests(unittest.TestCase):
    def _task(self) -> dict:
        return kc.task_identity(objective_identity="sha256:objective",
                                acceptance_identity="sha256:acceptance",
                                permissions_identity="sha256:permissions",
                                requirements_identity="sha256:requirements")

    def test_task_identity_and_delivered_identity_stay_distinct(self) -> None:
        task = self._task()
        delivered = select()["delivered_identity"]
        self.assertTrue(task["task_identity"].startswith("task:"))
        self.assertTrue(delivered.startswith("delivered-identity:"))
        self.assertNotIn(delivered, json.dumps(task))
        self.assertNotIn(task["task_identity"], delivered)
        # Changing only the task contract leaves the delivered knowledge identity alone.
        other_task = kc.task_identity(objective_identity="sha256:other",
                                      acceptance_identity="sha256:acceptance",
                                      permissions_identity="sha256:permissions",
                                      requirements_identity="sha256:requirements")
        self.assertNotEqual(task["task_identity"], other_task["task_identity"])
        self.assertEqual(delivered, select()["delivered_identity"])

    def test_F11_knowledge_change_while_a_hypothetical_waiter_waits(self) -> None:
        k1 = select()["delivered_identity"]
        k2 = select(contents={"project.architecture.overview": {"content": "architecture v2\n"}})["delivered_identity"]
        task = self._task()
        first = kc.active_work_equivalence_input(task=task, knowledge_identity=k1,
                                                 execution_compatibility={"surface": "codex-cli"})
        second = kc.active_work_equivalence_input(task=task, knowledge_identity=k2,
                                                  execution_compatibility={"surface": "codex-cli"})
        self.assertTrue(kc.equivalence_changed(first, second))
        self.assertEqual(first["task_identity"], second["task_identity"])
        self.assertEqual(k1, first["knowledge_identity"])
        stable = kc.active_work_equivalence_input(task=task, knowledge_identity=k1,
                                                  execution_compatibility={"surface": "codex-cli"})
        self.assertFalse(kc.equivalence_changed(first, stable))
        # Execution compatibility participates, without collapsing the two identities.
        other_surface = kc.active_work_equivalence_input(task=task, knowledge_identity=k1,
                                                         execution_compatibility={"surface": "opencode"})
        self.assertTrue(kc.equivalence_changed(first, other_surface))

    def test_REUSE_accepted_identity_must_match_the_current_validated_identity(self) -> None:
        k1 = select()["delivered_identity"]
        k2 = select(sources=[source(lifecycle="REVOKED")])["delivered_identity"]
        self.assertNotEqual(k1, k2)
        self.assertFalse(kc.reuse_eligible(accepted_knowledge_identity=k1, current_knowledge_identity=k2))
        self.assertTrue(kc.reuse_eligible(accepted_knowledge_identity=k1, current_knowledge_identity=k1))
        self.assertFalse(kc.reuse_eligible(accepted_knowledge_identity="", current_knowledge_identity=""))

    def test_no_master_b_surface_is_implemented(self) -> None:
        for attribute in ("claim", "acquire", "wait", "join", "lease", "heartbeat", "lock"):
            self.assertFalse(hasattr(kc, attribute), attribute)


class CrossAgentParityTests(unittest.TestCase):
    """F12 — equivalent requirement, equivalent authoritative semantic identity."""

    def test_equivalent_agents_produce_one_authoritative_identity(self) -> None:
        # Same authoritative sources/requirements/content; only the human-readable
        # selection reason differs (prompt wording lives outside identity).
        codex = document(reason="codex phrased this requirement")
        opencode = document(reason="opencode phrased this differently")
        parity = kc.semantic_parity(codex, opencode)
        self.assertTrue(parity["parity"])
        self.assertEqual([], parity["differences"])
        codex_result = kc.select(codex)
        opencode_result = kc.select(opencode)
        self.assertEqual(codex_result["manifest_fingerprint"], opencode_result["manifest_fingerprint"])
        self.assertEqual(codex_result["delivered_identity"], opencode_result["delivered_identity"])
        self.assertEqual(parity["semantic_fingerprint"], kc.semantic_parity(opencode, codex)["semantic_fingerprint"])
        self.assertEqual("codex phrased this requirement", codex_result["reason"])
        self.assertEqual("opencode phrased this differently", opencode_result["reason"])

    def test_reason_wording_does_not_change_identity_but_scope_does(self) -> None:
        self.assertEqual(select(reason="a")["delivered_identity"], select(reason="b")["delivered_identity"])
        self.assertNotEqual(select(scope="task_family:architecture_review")["delivered_identity"],
                            select(scope="task_family:code_review")["delivered_identity"])
        self.assertFalse(kc.semantic_parity(document(scope="one"), document(scope="two"))["parity"])
        self.assertEqual(["scope"], kc.semantic_parity(document(scope="one"),
                                                       document(scope="two"))["differences"])

    def test_agent_branding_cannot_enter_the_contract_identity(self) -> None:
        # Branding lives in the prompt wrapper, outside the contract document: a
        # document that carries it is invalid, so it cannot silently alter identity.
        branding = document()
        branding["agent"] = "codex"
        branding["prompt_wrapper"] = "You are Codex."
        result = kc.select(branding)
        self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE, result["outcome"])
        self.assertEqual(kc.UNKNOWN_FIELD, result["refusals"][0]["reason"])
        for field in ("agent", "prompt_wrapper"):
            self.assertNotIn(field, kc.DOCUMENT_FIELDS)

    def test_a_real_semantic_difference_is_reported(self) -> None:
        other = document(contents={"project.architecture.overview": {"content": "different\n"}})
        parity = kc.semantic_parity(document(), other)
        self.assertFalse(parity["parity"])
        self.assertEqual(["contents"], parity["differences"])
        authority_shift = document(sources=[source(authority="SUPPORTING")])
        self.assertFalse(kc.semantic_parity(document(), authority_shift)["parity"])
        self.assertEqual(["sources"], kc.semantic_parity(document(), authority_shift)["differences"])


if __name__ == "__main__":
    unittest.main()
