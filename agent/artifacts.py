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
    }
    if metrics["patch_attempts"]:
        metrics["patch_success_rate"] = round(
            metrics["patch_successes"] / metrics["patch_attempts"], 4
        )
    else:
        metrics["patch_success_rate"] = 0.0

    _write_json(artifact_dir / "events.json", events)
    _write_json(artifact_dir / "metrics.json", metrics)
    _write_json(artifact_dir / "result.json", result.to_dict())
    _write_json(artifact_dir / "retrievals.json", retrievals)
    _write_json(artifact_dir / "patches.json", patches)
    if manifest is not None:
        _write_json(artifact_dir / "run_manifest.json", manifest)
    if result.patch:
        (artifact_dir / "final_diff.patch").write_text(result.patch, encoding="utf-8")

    return artifact_dir


def _collect_retrievals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retrievals: list[dict[str, Any]] = []
    retrieval_tools = {"search_text", "find_files", "find_symbol", "graph_neighbors"}

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
