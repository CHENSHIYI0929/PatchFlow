"""
agent/artifacts.py

运行工件导出：
- events.json
- metrics.json
- result.json
- retrievals.json
- patches.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.event_log import EventLog, summarize_run
from agent.task import EventType, RunResult


def export_run_artifacts(
    log: EventLog,
    result: RunResult,
    elapsed_seconds: float,
    manifest: dict[str, Any] | None = None,
) -> Path:
    """
    为一次 run 导出结构化工件目录。
    """
    artifact_dir = log.path.parent / "artifacts" / log.path.stem
    artifact_dir.mkdir(parents=True, exist_ok=True)

    events = [event.to_dict() for event in log.replay()]
    stats = summarize_run(log)
    retrievals = _collect_retrievals(events)
    patches = _collect_patches(events, result.patch)
    grading = _collect_grading(events)
    capability_stats = _collect_capability_stats(events)
    materialized_manifest = _finalize_manifest(manifest, events)

    metrics = {
        **stats,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "steps_taken": result.steps_taken,
        "total_tokens": result.total_tokens,
        "task_success": result.is_success(),
        "tool_call_count": sum(stats["tool_calls"].values()),
        "retrieval_queries": len(retrievals),
        "retrieval_match_count": sum(item.get("match_count", 0) for item in retrievals),
        "patch_attempts": len(patches),
        "patch_successes": sum(1 for item in patches if item.get("success")),
        "patch_conflicts": sum(1 for item in patches if item.get("conflict")),
        "patch_reverts": sum(1 for item in patches if item.get("tool_name") == "revert_patch"),
        "graph_queries": sum(1 for item in retrievals if item.get("type") == "graph_neighbors"),
        "failure_type": result.failure_type,
        "failure_stage": result.failure_stage,
        "grader": grading.get("grader"),
        "grader_checks": grading.get("checks", []),
        **capability_stats,
    }
    if metrics["patch_attempts"]:
        metrics["patch_success_rate"] = round(
            metrics["patch_successes"] / metrics["patch_attempts"], 4
        )
    else:
        metrics["patch_success_rate"] = 0.0

    _write_json(artifact_dir / "events.json", events)
    (artifact_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    _write_json(artifact_dir / "metrics.json", metrics)
    _write_json(artifact_dir / "result.json", result.to_dict())
    _write_json(artifact_dir / "retrievals.json", retrievals)
    _write_json(artifact_dir / "patches.json", patches)
    (artifact_dir / "final_report.md").write_text(
        _render_final_report(result, metrics, retrievals, patches, events),
        encoding="utf-8",
    )
    if materialized_manifest is not None:
        _write_json(artifact_dir / "run_manifest.json", materialized_manifest)
    if result.patch:
        (artifact_dir / "final_diff.patch").write_text(result.patch, encoding="utf-8")

    return artifact_dir


def _collect_retrievals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retrievals: list[dict[str, Any]] = []
    retrieval_tools = {"search_text", "find_files", "find_symbol", "graph_neighbors", "hybrid_retrieval"}

    for event in events:
        if event["event_type"] == EventType.REPO_MAP.value:
            payload = event["payload"]
            trace = payload.get("trace", {})
            retrievals.append({
                "type": "repo_map",
                "budget": payload.get("budget"),
                "chunk_count": trace.get("chunk_count", 0),
                "omitted_files": trace.get("omitted_files", 0),
                "chunks": trace.get("chunks", []),
            })
            continue

        if event["event_type"] != EventType.OBSERVATION.value:
            continue
        obs = event["payload"]["observation"]
        if obs.get("tool_name") not in retrieval_tools:
            continue
        metadata = obs.get("metadata") or {}
        retrievals.append({
            "type": obs.get("tool_name"),
            "match_count": metadata.get("match_count", 0),
            "query": metadata.get("query"),
            "search_path": metadata.get("search_path"),
            "matches": metadata.get("matches", []),
            "step": event["payload"].get("step"),
            "step_id": event["payload"].get("step_id"),
        })

    return retrievals


def _collect_patches(events: list[dict[str, Any]], final_diff: str | None) -> list[dict[str, Any]]:
    patches: list[dict[str, Any]] = []
    for event in events:
        if event["event_type"] != EventType.OBSERVATION.value:
            continue
        obs = event["payload"]["observation"]
        tool_name = obs.get("tool_name")
        metadata = obs.get("metadata") or {}
        if tool_name == "apply_patch":
            error_text = f"{obs.get('error') or ''} {obs.get('output') or ''}"
            patches.append({
                "tool_name": tool_name,
                "step": event["payload"].get("step"),
                "step_id": event["payload"].get("step_id"),
                "success": obs.get("status") == "success",
                "patch": metadata.get("patch"),
                "edit_plan": metadata.get("edit_plan"),
                "stats": metadata.get("stats", {}),
                "reverse_patch": metadata.get("reverse_patch"),
                "conflict": "conflict" in error_text.lower(),
                "error": obs.get("error"),
            })
        elif tool_name == "revert_patch":
            patches.append({
                "tool_name": tool_name,
                "step": event["payload"].get("step"),
                "step_id": event["payload"].get("step_id"),
                "success": obs.get("status") == "success",
                "patch": metadata.get("patch"),
                "edit_plan": metadata.get("edit_plan"),
                "stats": metadata.get("stats", {}),
                "reverse_patch": metadata.get("reverse_patch"),
                "conflict": False,
                "error": obs.get("error"),
            })
        elif tool_name == "file_write":
            patches.append({
                "tool_name": tool_name,
                "step": event["payload"].get("step"),
                "step_id": event["payload"].get("step_id"),
                "success": obs.get("status") == "success",
                "patch": {
                    "patch_type": "replace_file",
                    "path": _extract_path_from_output(obs.get("output", "")),
                },
                "stats": {},
                "reverse_patch": None,
                "conflict": False,
                "error": obs.get("error"),
            })

    if final_diff:
        patches.append({
            "tool_name": "git_diff",
            "step": None,
            "step_id": None,
            "success": True,
            "patch": {"patch_type": "final_diff"},
            "stats": {"diff_present": True},
            "reverse_patch": None,
            "conflict": False,
            "error": None,
        })
    return patches


def _collect_grading(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event["event_type"] != EventType.OBSERVATION.value:
            continue
        obs = event["payload"]["observation"]
        if obs.get("tool_name") not in {"final_grader", "preflight_verify"}:
            continue
        metadata = obs.get("metadata") or {}
        return {
            "grader": metadata.get("grader"),
            "checks": metadata.get("checks", []),
        }
    return {"grader": None, "checks": []}


def _collect_capability_stats(events: list[dict[str, Any]]) -> dict[str, int]:
    observations = [
        event["payload"]["observation"]
        for event in events
        if event["event_type"] == EventType.OBSERVATION.value
    ]
    reflections = [
        event["payload"]
        for event in events
        if event["event_type"] == EventType.REFLECTION.value
    ]
    finish_verifier = [obs for obs in observations if obs.get("tool_name") == "finish_verifier"]
    self_reviews = [obs for obs in observations if obs.get("tool_name") == "self_review"]
    patch_reviews = [obs for obs in observations if obs.get("tool_name") == "patch_review"]
    symbol_probes = [
        obs for obs in observations
        if (obs.get("metadata") or {}).get("auto_symbol_probe")
    ]
    return {
        "finish_verification_attempts": len(finish_verifier),
        "finish_verification_failures": sum(1 for obs in finish_verifier if obs.get("status") != "success"),
        "self_review_attempts": len(self_reviews),
        "self_review_failures": sum(1 for obs in self_reviews if obs.get("status") != "success"),
        "taxonomy_recovery_prompts": sum(1 for item in reflections if item.get("reason") == "taxonomy_recovery"),
        "auto_symbol_probes": len(symbol_probes),
        "long_memory_hits": sum(1 for item in reflections if item.get("reason") == "long_memory"),
        "context_compressions": sum(1 for item in reflections if item.get("reason") == "context_compression"),
        "failure_analyses": sum(1 for obs in observations if obs.get("tool_name") == "failure_analyzer"),
        "edit_plans": sum(1 for item in reflections if item.get("reason") == "edit_plan"),
        "patch_review_attempts": len(patch_reviews),
        "patch_review_failures": sum(1 for obs in patch_reviews if obs.get("status") != "success"),
    }


def _render_final_report(
    result: RunResult,
    metrics: dict[str, Any],
    retrievals: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> str:
    def _patch_dict(item: dict[str, Any]) -> dict[str, Any]:
        patch = item.get("patch")
        return patch if isinstance(patch, dict) else {}

    def _edit_plan_dict(item: dict[str, Any]) -> dict[str, Any]:
        plan = item.get("edit_plan")
        return plan if isinstance(plan, dict) else {}

    modified = [
        _patch_dict(item).get("path")
        for item in patches
        if item.get("tool_name") in {"apply_patch", "file_write"} and item.get("patch")
    ]
    tests = [
        obs.get("output") or obs.get("error")
        for event in events
        if event["event_type"] == EventType.OBSERVATION.value
        for obs in [event["payload"]["observation"]]
        if obs.get("tool_name") in {"test", "pytest", "finish_verifier", "preflight_verify"}
    ]
    reviews = [
        event["payload"]["observation"]
        for event in events
        if event["event_type"] == EventType.OBSERVATION.value
        and event["payload"]["observation"].get("tool_name") in {"self_review", "patch_review"}
    ]
    lines = [
        "# Final Report",
        "",
        f"- Status: {result.status.value}",
        f"- Success: {result.is_success()}",
        f"- Summary: {result.summary}",
        f"- Failure type: {result.failure_type or 'none'}",
        f"- Failure stage: {result.failure_stage or 'none'}",
        f"- Steps: {result.steps_taken}",
        f"- Tokens: {result.total_tokens}",
        "",
        "## Modified Files",
        "",
        *[f"- {path}" for path in modified if path],
        "",
        "## Retrieval Candidates",
        "",
        *[
            f"- {match.get('path')} score={match.get('score')} reasons={', '.join(match.get('reasons', []))}"
            for item in retrievals
            if item.get("type") == "hybrid_retrieval"
            for match in item.get("matches", [])[:8]
        ],
        "",
        "## Edit Plans",
        "",
        *[
            f"- {_patch_dict(item).get('path')}: risk={_edit_plan_dict(item).get('risk_level')} intent={_edit_plan_dict(item).get('change_intent')}"
            for item in patches
            if item.get("edit_plan")
        ],
        "",
        "## Review Results",
        "",
        *[
            f"- {review.get('tool_name')}: {review.get('status')} {review.get('metadata', {}).get('findings', [])}"
            for review in reviews
        ],
        "",
        "## Test Results",
        "",
        *[f"- {str(test).splitlines()[0][:180]}" for test in tests if test],
        "",
        "## Rollback",
        "",
        "Use `agent patch latest --artifact-dir <artifact_dir>` to inspect the latest patch metadata, then `agent patch rollback --artifact-dir <artifact_dir> --repo <repo>` to replay its reverse patch when available.",
        "",
        "## Metrics",
        "",
        f"- failure_analyses: {metrics.get('failure_analyses', 0)}",
        f"- edit_plans: {metrics.get('edit_plans', 0)}",
        f"- patch_review_failures: {metrics.get('patch_review_failures', 0)}",
        f"- long_memory_hits: {metrics.get('long_memory_hits', 0)}",
        f"- context_compressions: {metrics.get('context_compressions', 0)}",
    ]
    return "\n".join(lines) + "\n"


def _finalize_manifest(
    manifest: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if manifest is None:
        return None

    materialized = dict(manifest)
    if events:
        materialized.setdefault("run_started_at", events[0].get("timestamp"))
        materialized.setdefault("run_finished_at", events[-1].get("timestamp"))
    return materialized


def _extract_path_from_output(output: str) -> str | None:
    marker = " to "
    if marker not in output:
        return None
    return output.split(marker, 1)[1].strip()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
