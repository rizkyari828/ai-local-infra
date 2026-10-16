"""Read-only command surface for Efficiency Telemetry V0.1.

Thin wrapper over ``scripts/common/efficiency.py``: every field printed comes from that
module, so nothing here grows the metric set. There are no new metric names, no cost or
token estimation, and no RTK/Headroom merge — this file only decides *what to ask for*
and *how to print it*, which is what makes a baseline captured before a pilot diffable
against one captured after it without a mapping layer.

Read-only: it reads the operational DB, never creates it, and never writes to the
operational store, the learning store or the result cache.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any, Optional

import efficiency
import infra
import orchestration_store

# The module's own bounded window for "recent runs"; stated rather than invented.
DEFAULT_LIMIT = 20
SCHEMA_VERSION = 1

# What a baseline deliberately omits. Each of these lives in a separate dimension that
# this module never merges in (see efficiency.py and docs/EFFICIENCY_TELEMETRY.md).
NOT_INCLUDED = ("tokens", "cost", "RTK savings", "Headroom savings",
                "prompts", "responses", "transcripts")


def _state_dir(args: argparse.Namespace) -> pathlib.Path:
    return pathlib.Path(args.state_dir) if args.state_dir else infra.STATE_DIR


def _open_store(state_dir: pathlib.Path) -> orchestration_store.OrchestrationStore:
    # A read-only command must not create the store it was asked to read.
    db = orchestration_store.db_path(state_dir)
    if not db.exists():
        raise SystemExit(f"efficiency: no operational DB at {db} (read-only; nothing was created)")
    return orchestration_store.OrchestrationStore(db=db)


def _render(obj: Any, indent: int = 0) -> None:
    """Plain text rendering of the module's own output; it adds no fields."""
    pad = "  " * indent
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)) and value:
                print(f"{pad}{key}:")
                _render(value, indent + 1)
            else:
                print(f"{pad}{key}: {value if not isinstance(value, (dict, list)) else 'none'}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                print(f"{pad}-")
                _render(item, indent + 1)
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{obj}")


def _emit(data: Any, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _render(data)


def _machine_id(summary: dict[str, Any]) -> Optional[str]:
    # The module reports machine_id per run and as the active filter; prefer the filter.
    if summary["machine_id"]:
        return summary["machine_id"]
    seen = {row["machine_id"] for row in summary["runs"] if row.get("machine_id")}
    return next(iter(seen)) if len(seen) == 1 else None


def cmd_summary(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    store = _open_store(state_dir)
    try:
        data = efficiency.get_recent_efficiency(store=store, state_dir=state_dir,
                                                limit=args.limit, machine_id=args.machine)
    finally:
        store.close()
    _emit(data, args)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    store = _open_store(state_dir)
    try:
        data = efficiency.get_run_efficiency(args.run_id, store=store, state_dir=state_dir)
    finally:
        store.close()
    _emit(data, args)
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    store = _open_store(state_dir)
    try:
        data = efficiency.get_model_usage(store=store, state_dir=state_dir, machine_id=args.machine)
    finally:
        store.close()
    _emit(data, args)
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    store = _open_store(state_dir)
    try:
        data = efficiency.get_cache_efficiency(store=store, state_dir=state_dir, machine_id=args.machine)
    finally:
        store.close()
    _emit(data, args)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    # Context metrics are bounded metadata files; no operational store is involved.
    data = efficiency.get_context_efficiency(args.run_id, state_dir=_state_dir(args))
    _emit(data, args)
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    store = _open_store(state_dir)
    try:
        summary = efficiency.get_recent_efficiency(store=store, state_dir=state_dir, limit=DEFAULT_LIMIT)
        models = efficiency.get_model_usage(store=store, state_dir=state_dir)
        cache = efficiency.get_cache_efficiency(store=store, state_dir=state_dir)
    finally:
        store.close()

    now = dt.datetime.now(dt.timezone.utc)
    document = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "machine_id": _machine_id(summary),
        "project_id": summary["project_id"],
        # Blocks are the module's own output, never reshaped: a later diff needs no mapping.
        "summary": summary,
        "models": models,
        "cache": cache,
        "source": {"state_dir": summary["state_dir"], "runs_included": summary["totals"]["runs"]},
        "not_included": list(NOT_INCLUDED),
    }
    path = (pathlib.Path(args.out) if args.out
            else state_dir / "efficiency" / f"baseline-{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(document, indent=2, ensure_ascii=False))
    else:
        print(f"baseline: {path}")
        _render(document)
    return 0


def build_parser() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--state-dir", type=pathlib.Path,
                      help="machine state root (default: $LAI_STATE_DIR)")
    base.add_argument("--json", action="store_true", help="machine-readable output")

    filtered = argparse.ArgumentParser(add_help=False, parents=[base])
    filtered.add_argument("--machine", help="filter by machine id")

    parser = argparse.ArgumentParser(
        prog="efficiency",
        description="Read-only efficiency telemetry over the operational store.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("summary", parents=[filtered], help="aggregate over the recent-runs window")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"runs to include (default {DEFAULT_LIMIT})")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("run", parents=[base], help="one run's efficiency record")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("models", parents=[filtered], help="model usage across runs")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("cache", parents=[filtered], help="cache efficiency across runs")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("context", parents=[base], help="context metrics for one run")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("baseline", parents=[base], help="capture a comparable baseline snapshot")
    p.add_argument("--out", type=pathlib.Path,
                   help="write here instead of $LAI_STATE_DIR/efficiency/")
    p.set_defaults(func=cmd_baseline)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except orchestration_store.StoreError as exc:
        print(f"efficiency: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
