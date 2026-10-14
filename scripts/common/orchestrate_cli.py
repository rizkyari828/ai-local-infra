#!/usr/bin/env python3
"""Task graph orchestrator CLI: plan (offline) and run (gated execution).

    ./scripts/orchestrate plan --objective "..." [--json] [--surface ...]
    ./scripts/orchestrate run  --objective "..." --confirm [--backend fake|opencode|codex|auto]
                               [--dry-run] [--resume] [--run-id ID] [--surface ...]
                               [--min-tier T] [--max-tier T] [--local-only] [--no-premium]
                               [--no-cache | --refresh] [--max-ai-calls N]
                               [--entry-mode chat-handoff|direct-cli|automation|api-or-ui]
    ./scripts/orchestrate direct --objective "..." [--confirm] [--backend ...] [run options]
    ./scripts/orchestrate efficiency [RUN_ID] [--recent] [--model-usage] [--json]
    ./scripts/orchestrate cache [--state-dir DIR] [--json]

plan is local/deterministic and persists nothing. run/direct execute the validated graph
sequentially through a provider-agnostic backend adapter; real backends require an
explicit --confirm gate. `direct` is the direct local entry path (no chat controller).
run reuses previously accepted results when inputs provably match (result cache; enabled
by default, `--refresh` bypasses reuse, `--no-cache` disables it). The raw objective is
processed in memory only and is never persisted. efficiency reads deterministic
telemetry from operational state (never CLI prose). See docs/ORCHESTRATION.md,
docs/EXECUTION.md, docs/RESULT_CACHE.md, and docs/EFFICIENCY_TELEMETRY.md.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import orchestrator  # noqa: E402
import execution  # noqa: E402
import authority  # noqa: E402
import discovery  # noqa: E402
import failover  # noqa: E402
import orchestration_store  # noqa: E402
import result_cache  # noqa: E402
import efficiency  # noqa: E402
import lifecycle  # noqa: E402
import human_acceptance  # noqa: E402
import infra  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _local_health() -> tuple[failover.Health, dict[str, "execution.Backend"]]:
    """Cheap, no-inference runtime availability for the configured backends.

    Machine identity/policy come from the one authority, so a machine that only
    declares ``provider_availability.ollama: false`` (without the
    ``local_inference`` marker) is also treated as prohibiting local inference, and an
    invalid policy refuses before any probe instead of probing as "permitted".
    """
    config = discovery.load_config()
    providers = {name: set(spec.get("providers", [])) for name, spec in config.get("backends", {}).items()}
    binaries = {name: bool(shutil.which(name)) for name in providers}
    try:
        policy = authority.machine_policy_or_fail()
    except authority.AuthorityError as exc:
        raise execution.ExecutionError(f"MACHINE_POLICY_INVALID: {exc}") from exc
    codex_model, codex_error = authority.read_codex_model()
    ollama_models: set[str] | None = None
    if "ollama" not in policy["prohibited"]:
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as response:
                ollama_models = {model["name"] for model in json.load(response).get("models", [])}
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            ollama_models = None  # unreachable -> UNKNOWN (never assumed available)
    health = failover.Health(binaries=binaries, providers=providers, ollama_models=ollama_models,
                             codex_model=codex_model, codex_model_error=codex_error,
                             local_inference=policy["local_inference"],
                             prohibited=policy["prohibited"])
    backends = {name: execution.CliBackend(name, name) for name in providers}
    return health, backends


def _resolve_workspace(args: argparse.Namespace) -> dict:
    try:
        return authority.resolve_workspace(getattr(args, "workspace", None))
    except authority.AuthorityError as exc:
        raise execution.ExecutionError(str(exc)) from exc


def _objective(args: argparse.Namespace) -> str:
    objective = args.objective
    if objective is None and args.objective_file:
        objective = pathlib.Path(args.objective_file).read_text(encoding="utf-8")
    return objective or ""


def _constraints(args: argparse.Namespace) -> dict:
    return {"surface": args.surface, "min_tier": args.min_tier, "max_tier": args.max_tier,
            "local_only": args.local_only, "no_premium": args.no_premium,
            "read_only": getattr(args, "read_only", False),
            "max_ai_calls": getattr(args, "max_ai_calls", None),
            "prefer_provider": getattr(args, "prefer_provider", None),
            "prefer_model": getattr(args, "prefer_model", None)}


def cmd_plan(args: argparse.Namespace) -> int:
    objective = _objective(args)
    if not objective.strip():
        print("ERROR: provide --objective or --objective-file", file=sys.stderr)
        return 2
    plan = orchestrator.decompose(objective, constraints=_constraints(args))
    plan["preferences"] = {"prefer_model": args.prefer_model, "prefer_provider": args.prefer_provider,
                           "note": "informational only; V0.1 never auto-switches models"}
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print(orchestrator.render_plan(plan))
    return 0 if plan["valid"] else 2


def cmd_run(args: argparse.Namespace) -> int:
    objective = _objective(args)
    if not objective.strip():
        print("ERROR: provide --objective or --objective-file", file=sys.stderr)
        return 2
    plan = orchestrator.decompose(objective, constraints=_constraints(args))
    if not plan["valid"]:
        print("ERROR: invalid plan; run 'plan' to inspect diagnostics", file=sys.stderr)
        return 2
    entry_mode = getattr(args, "entry_mode", None) or "DIRECT_CLI"
    controller_ai_calls = getattr(args, "controller_ai_calls", None)
    backend = execution.backend_for(args.backend)
    health, backends = _local_health()
    if args.backend != "fake":
        backends.setdefault(backend.name, backend)
    workspace = _resolve_workspace(args)
    report = execution.execute_plan(
        plan, backend=backend, run_id=args.run_id, state_dir=args.state_dir,
        repo_root=pathlib.Path(workspace["workspace"]), confirm=args.confirm, dry_run=args.dry_run,
        allow_dirty=args.allow_dirty, surface=args.surface, record_evidence=not args.no_evidence,
        resume=args.resume, read_only=args.read_only, max_ai_calls=args.max_ai_calls,
        backends=backends, health=(None if args.backend == "fake" else health),
        constraints=_constraints(args), cache=not args.no_cache,
        force_refresh=args.refresh,
        explicit_model=bool(args.prefer_model or args.prefer_provider),
        entry_mode=entry_mode, controller_ai_calls=controller_ai_calls)
    if not args.no_store:
        # Automatic lifecycle hooks: run/task/attempt/events/outcome evidence/context.
        lifecycle.record_run(plan, report, state_dir=args.state_dir,
                             machine_id=os.environ.get("LAI_MACHINE") or "unknown",
                             entry_mode=entry_mode, controller_ai_calls=controller_ai_calls)
    if args.json:
        print(json.dumps({**report, "workspace": workspace}, indent=2, ensure_ascii=False))
    else:
        print(_render_workspace(workspace))
        print(execution.render_execution(report))
    if report.get("error"):
        return 2
    final = report.get("final") or {}
    return 1 if final.get("failed") or final.get("unverified") or final.get("cancelled") else 0


def _tier(value: str) -> str:
    candidate = value.upper()
    if candidate not in orchestrator.TIER_ORDER:
        raise argparse.ArgumentTypeError(f"tier must be one of {', '.join(orchestrator.TIER_ORDER)}")
    return candidate


def _render_workspace(workspace: dict) -> str:
    line = (f"WORKSPACE {workspace['workspace']}  source={workspace['source']}  "
            f"repo={workspace['repository']['id']}")
    conflict = workspace.get("conflict")
    if conflict:
        line += (f"  CONFLICT explicit={conflict['explicit_identity']} "
                 f"discovered={conflict['discovered_identity']}")
    return line


def _entry_mode(value: str) -> str:
    candidate = value.strip().upper().replace("-", "_")
    if candidate not in orchestration_store.ENTRY_MODES:
        raise argparse.ArgumentTypeError(
            f"entry mode must be one of {', '.join(orchestration_store.ENTRY_MODES)}")
    return candidate


def _add_objective_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--objective")
    parser.add_argument("--objective-file")
    parser.add_argument("--surface", help="target surface for capability resolution")
    parser.add_argument("--min-tier", type=_tier, help="floor every node at this tier")
    parser.add_argument("--max-tier", type=_tier, help="cap nodes (never below a safety floor)")
    parser.add_argument("--local-only", action="store_true", help="prefer local/economical (cap BALANCED)")
    parser.add_argument("--no-premium", action="store_true", help="disallow MAX unless a floor requires it")
    parser.add_argument("--prefer-model", help="informational preference (no auto-switch in V0.1)")
    parser.add_argument("--prefer-provider", help="informational preference (no auto-switch in V0.1)")
    parser.add_argument("--read-only", action="store_true",
                        help="hard read-only policy: reject any mutating node before execution")
    parser.add_argument("--max-ai-calls", type=int,
                        help="hard ceiling on real AI worker launches (also drives budget-aware planning)")
    parser.add_argument("--json", action="store_true")


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", default="auto", help="fake | opencode | codex | auto")
    parser.add_argument("--dry-run", action="store_true", help="show resolved backends/calls; launch nothing")
    parser.add_argument("--confirm", action="store_true", help="required gate for real execution")
    parser.add_argument("--allow-dirty", action="store_true", help="allow a dirty worktree (bounded override)")
    parser.add_argument("--resume", action="store_true", help="resume a previous run (--run-id required)")
    parser.add_argument("--run-id", help="run identifier")
    parser.add_argument("--state-dir", type=pathlib.Path, help="override local state root")
    parser.add_argument("--workspace", type=pathlib.Path,
                        help="explicit workspace root (default: the invocation repository's toplevel)")
    parser.add_argument("--no-evidence", action="store_true", help="do not record outcome evidence")
    parser.add_argument("--no-store", action="store_true", help="do not persist operational run state")
    parser.add_argument("--no-cache", action="store_true",
                        help="disable result-cache reuse and writes for this run")
    parser.add_argument("--refresh", action="store_true",
                        help="bypass reuse for this run (cache entries are kept, not deleted)")
    parser.add_argument("--entry-mode", type=_entry_mode, default="DIRECT_CLI",
                        help="how the run started (default direct-cli); a chat controller uses chat-handoff")
    parser.add_argument("--controller-ai-calls", type=int,
                        help="trusted controller AI-call count for chat-handoff (never inferred)")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="produce a validated execution plan (no execution)")
    _add_objective_args(plan)
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="execute the validated plan sequentially (gated)")
    _add_objective_args(run)
    _add_run_args(run)
    run.set_defaults(func=cmd_run)

    direct = sub.add_parser("direct", help="direct local entry path (no chat controller)")
    _add_objective_args(direct)
    _add_run_args(direct)
    direct.set_defaults(func=cmd_run)

    efficiency_cmd = sub.add_parser("efficiency", help="deterministic run efficiency telemetry (read-only)")
    efficiency_cmd.add_argument("run_id", nargs="?", help="a specific run id (omit for --recent)")
    efficiency_cmd.add_argument("--recent", action="store_true", help="summarize recent runs")
    efficiency_cmd.add_argument("--model-usage", action="store_true", help="show model/backend usage groups")
    efficiency_cmd.add_argument("--limit", type=int, default=20, help="recent run limit")
    efficiency_cmd.add_argument("--machine-id", help="filter by machine id")
    efficiency_cmd.add_argument("--project-id", help="filter by project/repository id")
    efficiency_cmd.add_argument("--state-dir", type=pathlib.Path, help="override local state root")
    efficiency_cmd.add_argument("--json", action="store_true")
    efficiency_cmd.set_defaults(func=cmd_efficiency)

    cache = sub.add_parser("cache", help="show local result-cache status (read-only)")
    cache.add_argument("--state-dir", type=pathlib.Path, help="override local state root")
    cache.add_argument("--json", action="store_true")
    cache.set_defaults(func=cmd_cache)

    accept = sub.add_parser("accept", help="record explicit human acceptance for a judgment result")
    accept.add_argument("--run-id", required=True)
    accept.add_argument("--task-id", required=True)
    accept.add_argument("--reviewer")
    decision = accept.add_mutually_exclusive_group()
    decision.add_argument("--accept", action="store_true")
    decision.add_argument("--reject", action="store_true")
    accept.add_argument("--status", action="store_true", help="show the recorded decision (read-only)")
    accept.add_argument("--note")
    accept.add_argument("--state-dir", type=pathlib.Path, help="override local state root")
    accept.add_argument("--json", action="store_true")
    accept.set_defaults(func=cmd_accept)
    return result


def _render_efficiency(data: dict, args: argparse.Namespace) -> str:
    if args.model_usage:
        lines = [f"MODEL USAGE  total_invocations={data['total_invocations']}"]
        for row in data["groups"]:
            lines.append(f"  {row['backend']}/{row['provider']}/{row['model']} "
                         f"{row['required_tier']}/{row['required_effort']} native={row['native_effort']} "
                         f"normalized={row['normalized_effort']} {row['status']} "
                         + (f"({row['failure_category']}) " if row['failure_category'] else "")
                         + f"x{row['count']}")
        return "\n".join(lines)
    if "totals" in data:
        t = data["totals"]
        lines = [f"RECENT EFFICIENCY  runs={t['runs']}  logical_ai={t['logical_ai_tasks']}  "
                 f"cache_reused_ai={t['cache_reused_ai_tasks']}  "
                 f"real_invocations={t['real_ai_invocation_attempts']}  "
                 f"tool_only={t['tool_only_tasks']}  true_zero_ai_runs={t['true_zero_ai_runs']}"]
        for run in data["runs"]:
            lines.append(f"  {run['run_id']}  {run['entry_mode']}  {run['status']}  "
                         f"ai {run['ai']['real_invocation_attempts']}  "
                         f"cache {run['tasks']['cache_reused_ai']}  "
                         f"true_zero_ai={run['zero_ai']['true_zero_ai']}")
        return "\n".join(lines)
    t, a, z = data["tasks"], data["ai"], data["zero_ai"]
    lines = [f"RUN {data['run_id']}  entry={data['entry_mode']}  status={data['status']}  "
             f"planner={data['planner_mode']}",
             f"TASKS  total={t['total']} tool_only={t['tool_only']} no_change={t['no_change']} "
             f"logical_ai={t['logical_ai']} cache_reused_ai={t['cache_reused_ai']} fresh_ai={t['fresh_ai']}",
             f"AI  real_invocations={a['real_invocation_attempts']} successful={a['successful_invocations']} "
             f"failed={a['failed_invocations']} retries={a['retry_calls']} fallbacks={a['fallback_calls']} "
             f"planner_calls={a['planner_calls']} calls_avoided_by_cache={a['calls_avoided_by_cache']}",
             f"CACHE  hits={data['cache']['hits']} misses={data['cache']['misses']} "
             f"rejected={data['cache']['rejected']} invalid={data['cache']['invalid']} "
             f"bypassed={data['cache']['bypassed_by_user']}",
             f"BUDGET  hard_max={data['budget']['hard_max_ai_calls']} "
             f"remaining={data['budget']['remaining_ai_call_budget']}",
             f"ZERO-AI  local_ai_calls={z['local_ai_calls']} "
             f"controller_ai_calls={z['controller_ai_calls']} "
             f"zero_additional_ai={z['zero_additional_ai']} true_zero_ai={z['true_zero_ai']}",
             f"CONTEXT  packs={data['context']['pack_count']} chars={data['context']['total_chars']} "
             f"required={data['context']['required_items']} optional={data['context']['optional_items']} "
             f"dropped={data['context']['optional_items_dropped']} "
             f"dedup_chars={data['context']['deduplicated_chars']}"]
    return "\n".join(lines)


def cmd_efficiency(args: argparse.Namespace) -> int:
    try:
        if args.model_usage:
            data = efficiency.get_model_usage(state_dir=args.state_dir, run_id=args.run_id,
                                              machine_id=args.machine_id, project_id=args.project_id)
        elif args.run_id:
            data = efficiency.get_run_efficiency(args.run_id, state_dir=args.state_dir)
        else:
            data = efficiency.get_recent_efficiency(state_dir=args.state_dir, limit=args.limit,
                                                    machine_id=args.machine_id, project_id=args.project_id)
    except orchestration_store.StoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else _render_efficiency(data, args))
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.state_dir or infra.STATE_DIR) / "orchestration" / args.run_id
    state, status = execution._load_resume_state(run_dir)
    if status != execution._RESUME_OK:
        print("ERROR: no durable run state for this run; nothing to accept", file=sys.stderr)
        return 2
    node = (state.get("nodes") or {}).get(args.task_id)
    if not isinstance(node, dict):
        print(f"ERROR: unknown task {args.task_id!r} in run {args.run_id!r}", file=sys.stderr)
        return 2
    delivered = node.get("resume_input_identity") or node.get("input_fingerprint")
    result_identity = node.get("result_fingerprint")
    contract_identity = state.get("plan_fingerprint")
    if not (delivered and result_identity and contract_identity):
        print("ERROR: result is not independently identifiable (missing contract/input/result "
              "identity); explicit human acceptance is not possible", file=sys.stderr)
        return 2
    if args.status:
        entry = human_acceptance.load(run_id=args.run_id, task_id=args.task_id, state_dir=args.state_dir)
        print(json.dumps(entry, indent=2, ensure_ascii=False) if args.json else (
            f"HUMAN ACCEPTANCE  {args.run_id}/{args.task_id}: "
            + (entry.get("decision", "NONE") if entry else "NONE")
            + (f"  reviewer={entry.get('reviewer')}  at {entry.get('accepted_at')}" if entry else "")))
        return 0
    if not args.accept and not args.reject:
        print("ERROR: choose --accept or --reject (or --status)", file=sys.stderr)
        return 2
    if not args.reviewer:
        print("ERROR: --reviewer is required to record acceptance", file=sys.stderr)
        return 2
    try:
        entry = human_acceptance.record(
            run_id=args.run_id, task_id=args.task_id,
            decision=human_acceptance.ACCEPTED if args.accept else human_acceptance.REJECTED,
            reviewer=args.reviewer, task_contract_identity=contract_identity,
            delivered_input_identity=delivered, result_identity=result_identity,
            note=args.note, state_dir=args.state_dir)
    except human_acceptance.HumanAcceptanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(entry, indent=2, ensure_ascii=False) if args.json else (
        f"HUMAN {entry['decision']}  {args.run_id}/{args.task_id}  reviewer={entry['reviewer']}"))
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    try:
        store = result_cache.ResultCache(state_dir=args.state_dir)
    except result_cache.CacheError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        status = store.status()
    finally:
        store.close()
    print(json.dumps(status, indent=2, ensure_ascii=False) if args.json else (
        f"RESULT CACHE  {status['db_path']}\n"
        f"schema {status['schema_version']}  entries {status['entries']}  "
        f"size {status['db_size_bytes']} bytes  oldest {status['oldest']}  newest {status['newest']}"))
    return 0


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except (orchestrator.OrchestratorError, execution.ExecutionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
