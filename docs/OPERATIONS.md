# Operations

## Inspect and apply

Start with `./scripts/ai-profile status` or compare a profile explicitly with `--profile coding-routine`. `status` reports `MATCH`, `DRIFT`, or `UNKNOWN` for Qwen and Ollama and never repairs anything.

`./scripts/ai-profile use coding-routine --dry-run` prints active/desired profiles, safe current/desired projections, changed files, loaded-model conflicts, and restart need. It never prints the Qwen `env` object or provider credentials.

Real apply validates first. Qwen changes remain user-level. If the Ollama override differs, `sudo` is used only for install, daemon reload, restart, and a possible system-file restore. A different loaded heavyweight model stops apply; `--unload` explicitly authorizes `ollama stop` for conflicts. The command does not preload the requested model.

Backups are timestamped beneath `~/.local/state/local-ai-infra/backups/`. `rollback --dry-run` previews the newest backup; `rollback` restores it and restarts Ollama only if the system override was part of that backup. Keep this directory private because Qwen backups may contain credentials.

## Telemetry storage and hygiene

v0.1 writes append-only JSONL. Each event is a flat, typed record with stable IDs, UTC time, event type, dimensions (project/client/model/profile/operation), numeric measures, outcome fields, and explicit runtime/config versions. The teacher/student extension adds `telemetry/escalations/runs.jsonl` with the same flat, null-capable shape, defined by `telemetry/schemas/escalation-run.schema.json`. See [Teacher/student learning loop](TEACHER_STUDENT_LOOP.md). This shape is intentionally friendly to columnar conversion:

```text
JSONL (capture/source of truth for v0.1) -> Parquet (later compact analytics files) -> DuckDB (local ad-hoc analytics)
```

DuckDB is not installed or required. A future user could query JSONL directly or converted Parquet:

```sql
-- Illustrative future command, not a v0.1 dependency.
SELECT model, profile, count(*) AS runs, avg(duration_ms) AS avg_ms
FROM read_json_auto('telemetry/runs/*.jsonl', format = 'newline_delimited')
WHERE status = 'succeeded'
GROUP BY model, profile;

SELECT model, quantile_cont(duration_ms, 0.95) AS p95_ms
FROM read_parquet('telemetry/parquet/*.parquet')
GROUP BY model;
```

Avoid dynamic nested payloads in core events. Add fields through a new schema version, keep old readers possible, and use stable null-capable columns rather than changing meanings.

Retention guidance:

- Raw tool/runtime events: short-to-medium retention, then aggregate or delete deliberately.
- Recurring errors: group/deduplicate by fingerprint; retain reviewed pattern summaries longer than duplicates.
- Benchmark/eval summaries: long-lived with model/profile/hardware/version evidence.
- Accepted reviewed training candidates and decisions: long-lived and isolated by consent.
- Full prompts/source: not captured by default.
- Secrets, keys, tokens, `.env`, SSH material, personal files, and production-sensitive data: never intentionally captured.

## Future storage split (documented, not implemented)

- PostgreSQL may later hold operational/knowledge metadata, evaluations, accepted training examples, review/promotion decisions, and validated knowledge metadata.
- ClickHouse is only justified for future high-volume telemetry after real multi-agent or multi-machine event volume demonstrates the need.
- PostgreSQL + pgvector is only justified if historical knowledge grows enough to require semantic retrieval.
- Redis/Valkey remains deferred until caching, coordination, or rate-limiting has a measured need.

JSONL remains authoritative for this milestone. There is no migration code or runtime dependency for any future store.

## Failure handling

If apply validation fails after writes, the command attempts immediate restore. Inspect both the error and actual state with `status`; do not assume rollback succeeded after a power loss or failed `sudo`. `verify` checks repository syntax, active-state drift, and Ollama health. It does not run inference.
