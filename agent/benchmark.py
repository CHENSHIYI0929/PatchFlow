"""
agent/benchmark.py

Utilities for benchmark task specs and exported run artifact summaries.
"""

from __future__ import annotations

import json
import hashlib
import os
import platform
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.artifacts import export_run_artifacts
from agent.event_log import EventLog
from agent.grader import CommandGrader, CompositeGrader, Grader
from agent.task import Observation, ObservationStatus, RunResult, RunStatus, Task
from tools.runtime import LocalRuntime, Runtime


@dataclass
class BenchmarkTaskSpec:
    """A single benchmark task file plus its optional execution metadata."""

    path: Path
    description: str
    repo: str | None = None
    test_path: str | None = None
    test_cmd: str | None = None
    lint_cmd: str | None = None
    patch_policy_cmd: str | None = None
    exclude_paths: list[str] | None = None
    target_files: list[str] | None = None
    max_steps: int | None = None
    finish_if_verified: bool = True
    skip_preverified: bool = True


def load_task_spec(task_file: str | Path) -> BenchmarkTaskSpec:
    """
    Load a benchmark task file.

    Supports either:
    - plain-text task descriptions
    - a simple front-matter block:
        ---
        repo: demo/flash_demo
        test_path: test_buggy_math.py
        finish_if_verified: true
        ---
        Fix the bug...
    """
    path = Path(task_file)
    raw = path.read_text(encoding="utf-8")
    metadata, description = _split_front_matter(raw)

    return BenchmarkTaskSpec(
        path=path,
        description=description.strip(),
        repo=metadata.get("repo"),
        test_path=metadata.get("test_path"),
        test_cmd=metadata.get("test_cmd"),
        lint_cmd=metadata.get("lint_cmd"),
        patch_policy_cmd=metadata.get("patch_policy_cmd"),
        exclude_paths=_coerce_list(metadata.get("exclude_paths")),
        target_files=_coerce_list(metadata.get("target_files")),
        max_steps=_coerce_int(metadata.get("max_steps")),
        finish_if_verified=_coerce_bool(metadata.get("finish_if_verified"), default=True),
        skip_preverified=_coerce_bool(metadata.get("skip_preverified"), default=True),
    )


def resolve_task_repo(base_repo: str | Path, spec: BenchmarkTaskSpec) -> Path:
    """Resolve a task's effective repo path relative to the benchmark root repo."""
    base = Path(base_repo).resolve()
    if not spec.repo:
        return base
    return (base / spec.repo).resolve()


def default_test_cmd_for_spec(spec: BenchmarkTaskSpec) -> str | None:
    """Derive a concrete verification command from a spec."""
    if spec.test_cmd:
        return spec.test_cmd
    if spec.test_path:
        return f"python -m pytest {spec.test_path} --tb=short --no-header -q"
    return None


def build_grader_for_spec(spec: BenchmarkTaskSpec) -> Grader | None:
    graders: list[Grader] = []
    test_cmd = default_test_cmd_for_spec(spec)
    if test_cmd:
        graders.append(CommandGrader("test", test_cmd))
    if spec.lint_cmd:
        graders.append(CommandGrader("lint", spec.lint_cmd))
    if spec.patch_policy_cmd:
        graders.append(CommandGrader("patch_policy", spec.patch_policy_cmd))
    if not graders:
        return None
    if len(graders) == 1:
        return graders[0]
    return CompositeGrader(graders)


def try_preverify_task(
    spec: BenchmarkTaskSpec,
    repo_path: str | Path,
    *,
    log_dir: str,
    manifest: dict[str, Any] | None = None,
    runtime: Runtime | None = None,
    timeout: int = 120,
) -> tuple[RunResult, Path] | None:
    """
    If the task's target verification already passes, create a zero-token artifact
    and skip the LLM run.
    """
    grader = build_grader_for_spec(spec)
    if not spec.skip_preverified or not spec.finish_if_verified or grader is None:
        return None

    repo = Path(repo_path).resolve()
    exec_runtime = runtime or LocalRuntime()
    t0 = time.time()
    verification = grader.run(repo, runtime=exec_runtime, timeout=timeout)
    elapsed = time.time() - t0
    if not verification.success:
        return None

    task = Task(
        description=spec.description,
        repo_path=str(repo),
        source_repo_path=str(repo),
        task_id=str(uuid.uuid4())[:8],
        test_cmd=default_test_cmd_for_spec(spec),
        exclude_paths=spec.exclude_paths or [],
        target_files=spec.target_files or [],
        finish_if_verified=True,
        max_steps=spec.max_steps or 0,
    )
    summary = (
        "Skipped agent run because the targeted verification already passes in the task repo."
    )
    result = RunResult(
        task_id=task.task_id,
        status=RunStatus.SUCCESS,
        summary=summary,
        steps_taken=0,
        total_tokens=0,
    )
    with EventLog.create(task, log_dir=log_dir) as log:
        log.log_task_start(task)
        log.log_observation(
            step=0,
            observation=Observation(
                status=ObservationStatus.SUCCESS,
                output=verification.output.strip(),
                tool_name="preflight_verify",
                metadata={
                    "verification_cmd": verification.command,
                    "cwd": str(repo),
                    "preflight_verified": True,
                    "grader": grader.describe(),
                    "checks": verification.checks,
                },
            ),
        )
        log.log_task_complete(steps=0, summary=summary)
        artifact_dir = export_run_artifacts(log, result, elapsed, manifest=manifest)

    _mark_preverified_artifact(
        artifact_dir,
        verification_cmd=verification.command or "",
        cwd=str(repo),
        grader=grader.describe(),
        checks=verification.checks,
    )
    return result, artifact_dir


def prepare_clean_workspace(
    source_repo: str | Path,
    *,
    workspace_root: str | Path,
    task_name: str,
) -> Path:
    source = Path(source_repo).resolve()
    root = Path(workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_name).strip("_")
    workspace = root / f"{safe_name or 'task'}_{uuid.uuid4().hex[:8]}"
    shutil.copytree(
        source,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "logs", "artifacts"),
    )
    return workspace


def build_run_manifest(
    *,
    task_id: str,
    task_file: str | None,
    task_repo: str | Path,
    source_repo: str | Path,
    workspace_repo: str | Path,
    config: Any | None,
    grader: Grader | None,
    sandbox: bool,
) -> dict[str, Any]:
    task_file_path = Path(task_file).resolve() if task_file else None
    return {
        "run_id": task_id,
        "task_id": task_id,
        "task_file": str(task_file_path) if task_file_path else None,
        "task_version": _hash_file(task_file_path) if task_file_path else None,
        "run_started_at": None,
        "run_finished_at": None,
        "repo_source": str(Path(source_repo).resolve()),
        "task_repo": str(Path(task_repo).resolve()),
        "workspace_repo": str(Path(workspace_repo).resolve()),
        "model_provider": getattr(getattr(config, "llm", None), "provider", None),
        "model_name": getattr(getattr(config, "llm", None), "model", None),
        "agent_config": {
            "max_steps": getattr(getattr(config, "agent", None), "max_steps", None),
            "budget_tokens": getattr(getattr(config, "agent", None), "budget_tokens", None),
        },
        "grader_config": grader.describe() if grader else None,
        "code_version": _detect_code_version(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "runtime_type": "docker" if sandbox else "local",
        "sandbox_enabled": sandbox,
        "runtime": {
            "sandbox_enabled": sandbox,
            "runtime_type": "docker" if sandbox else "local",
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cwd": os.getcwd(),
        },
    }


def discover_artifact_dirs(root: str | Path) -> list[Path]:
    """Return artifact subdirectories that contain a metrics.json file."""
    root_path = Path(root)
    if not root_path.exists():
        return []
    return sorted(
        [path for path in root_path.iterdir() if path.is_dir() and (path / "metrics.json").exists()]
    )


def load_metrics(artifact_dir: str | Path) -> dict[str, Any]:
    """Load metrics.json from a single artifact directory."""
    artifact_path = Path(artifact_dir)
    return json.loads((artifact_path / "metrics.json").read_text(encoding="utf-8"))


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_artifacts(root: str | Path, *, include_preverified: bool = True) -> dict[str, Any]:
    """Aggregate metrics across all artifact runs under *root*."""
    artifact_dirs = discover_artifact_dirs(root)
    run_pairs = []
    for path in artifact_dirs:
        metrics = load_metrics(path)
        result = _load_json_if_exists(path / "result.json")
        manifest = _load_json_if_exists(path / "run_manifest.json")
        run_pairs.append((path, metrics, result, manifest))
    if not include_preverified:
        run_pairs = [
            (path, metrics, result, manifest)
            for path, metrics, result, manifest in run_pairs
            if not metrics.get("preflight_verified")
        ]
    artifact_dirs = [path for path, *_ in run_pairs]
    runs = [metrics for _, metrics, _, _ in run_pairs]
    results = [result for _, _, result, _ in run_pairs]
    manifests = [manifest for _, _, _, manifest in run_pairs]

    summary = {
        "artifact_root": str(Path(root)),
        "include_preverified": include_preverified,
        "run_count": len(runs),
        "success_count": 0,
        "success_rate": 0.0,
        "preverified_count": 0,
        "avg_steps": 0.0,
        "avg_tokens": 0.0,
        "avg_elapsed_seconds": 0.0,
        "avg_tool_calls": 0.0,
        "avg_retrieval_queries": 0.0,
        "avg_retrieval_match_count": 0.0,
        "avg_patch_attempts": 0.0,
        "patch_success_rate": 0.0,
        "patch_conflicts": 0,
        "patch_reverts": 0,
        "avg_graph_queries": 0.0,
        "failure_type_distribution": {},
        "failure_stage_distribution": {},
        "runs": [],
    }
    if not runs:
        return summary

    success_count = sum(1 for item in runs if item.get("task_success"))
    patch_attempts = sum(int(item.get("patch_attempts", 0)) for item in runs)
    patch_successes = sum(int(item.get("patch_successes", 0)) for item in runs)
    patch_conflicts = sum(int(item.get("patch_conflicts", 0)) for item in runs)
    patch_reverts = sum(int(item.get("patch_reverts", 0)) for item in runs)
    preverified_count = sum(1 for item in runs if item.get("preflight_verified"))
    failure_types: dict[str, int] = {}
    failure_stages: dict[str, int] = {}
    for result in results:
        failure_type = result.get("failure_type")
        if failure_type:
            failure_types[failure_type] = failure_types.get(failure_type, 0) + 1
        failure_stage = result.get("failure_stage")
        if failure_stage:
            failure_stages[failure_stage] = failure_stages.get(failure_stage, 0) + 1

    summary.update(
        {
            "success_count": success_count,
            "success_rate": round(success_count / len(runs), 4),
            "preverified_count": preverified_count,
            "avg_steps": round(sum(int(item.get("steps_taken", 0)) for item in runs) / len(runs), 3),
            "avg_tokens": round(sum(int(item.get("total_tokens", 0)) for item in runs) / len(runs), 3),
            "avg_elapsed_seconds": round(
                sum(float(item.get("elapsed_seconds", 0.0)) for item in runs) / len(runs), 3
            ),
            "avg_tool_calls": round(
                sum(int(item.get("tool_call_count", 0)) for item in runs) / len(runs), 3
            ),
            "avg_retrieval_queries": round(
                sum(int(item.get("retrieval_queries", 0)) for item in runs) / len(runs), 3
            ),
            "avg_retrieval_match_count": round(
                sum(int(item.get("retrieval_match_count", 0)) for item in runs) / len(runs), 3
            ),
            "avg_patch_attempts": round(
                sum(int(item.get("patch_attempts", 0)) for item in runs) / len(runs), 3
            ),
            "avg_graph_queries": round(
                sum(int(item.get("graph_queries", 0)) for item in runs) / len(runs), 3
            ),
            "patch_success_rate": round(
                (patch_successes / patch_attempts), 4
            ) if patch_attempts else 0.0,
            "patch_conflicts": patch_conflicts,
            "patch_reverts": patch_reverts,
            "failure_type_distribution": failure_types,
            "failure_stage_distribution": failure_stages,
            "runs": [
                {
                    "artifact_dir": str(path),
                    "task_id": result.get("task_id"),
                    "task_success": bool(metrics.get("task_success")),
                    "steps_taken": int(metrics.get("steps_taken", 0)),
                    "total_tokens": int(metrics.get("total_tokens", 0)),
                    "elapsed_seconds": float(metrics.get("elapsed_seconds", 0.0)),
                    "patch_attempts": int(metrics.get("patch_attempts", 0)),
                    "patch_successes": int(metrics.get("patch_successes", 0)),
                    "patch_conflicts": int(metrics.get("patch_conflicts", 0)),
                    "patch_reverts": int(metrics.get("patch_reverts", 0)),
                    "graph_queries": int(metrics.get("graph_queries", 0)),
                    "preflight_verified": bool(metrics.get("preflight_verified")),
                    "failure_type": result.get("failure_type"),
                    "failure_stage": result.get("failure_stage"),
                    "failure_message": result.get("failure_message"),
                    "summary": result.get("summary"),
                    "task_file": manifest.get("task_file"),
                    "workspace_repo": manifest.get("workspace_repo"),
                    "repo_source": manifest.get("repo_source"),
                    "patch_path": str(path / "final_diff.patch") if (path / "final_diff.patch").exists() else None,
                }
                for path, metrics, result, manifest in zip(artifact_dirs, runs, results, manifests)
            ],
        }
    )
    return summary


def compare_artifact_roots(
    left: str | Path,
    right: str | Path,
    *,
    include_preverified: bool = True,
) -> dict[str, Any]:
    """Compare aggregate metrics between two artifact roots."""
    left_summary = summarize_artifacts(left, include_preverified=include_preverified)
    right_summary = summarize_artifacts(right, include_preverified=include_preverified)

    delta_fields = [
        "run_count",
        "success_count",
        "success_rate",
        "preverified_count",
        "avg_steps",
        "avg_tokens",
        "avg_elapsed_seconds",
        "avg_tool_calls",
        "avg_retrieval_queries",
        "avg_retrieval_match_count",
        "avg_patch_attempts",
        "avg_graph_queries",
        "patch_success_rate",
        "patch_conflicts",
        "patch_reverts",
    ]

    deltas = {}
    for field in delta_fields:
        deltas[field] = round(
            float(right_summary.get(field, 0.0)) - float(left_summary.get(field, 0.0)),
            4,
        )

    return {
        "left": left_summary,
        "right": right_summary,
        "delta": deltas,
    }


def reset_benchmark_fixtures(
    repo_root: str | Path,
    *,
    fixtures_dir: str = "benchmark_fixtures",
    baseline_dir_name: str = "_baseline",
) -> Path:
    """Restore benchmark fixtures from the checked-in baseline snapshot."""
    root = Path(repo_root).resolve()
    fixtures_root = root / fixtures_dir
    baseline_root = fixtures_root / baseline_dir_name
    if not baseline_root.exists():
        raise FileNotFoundError(f"Fixture baseline not found: {baseline_root}")

    for entry in baseline_root.iterdir():
        if not entry.is_dir():
            continue
        target = fixtures_root / entry.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(entry, target)

    return fixtures_root


def replay_patch_from_artifact(
    artifact_dir: str | Path,
    repo_root: str | Path,
    *,
    patch_index: int = -1,
    reverse: bool = False,
) -> dict[str, Any]:
    """
    Replay a structured patch from patches.json onto repo_root.
    If reverse=True, apply the recorded reverse_patch instead.
    """
    from tools.file_tool import ApplyPatchTool

    artifact_path = Path(artifact_dir).resolve()
    repo = Path(repo_root).resolve()
    patches = json.loads((artifact_path / "patches.json").read_text(encoding="utf-8"))
    candidates = [
        item for item in patches
        if item.get("tool_name") in {"apply_patch", "revert_patch"} and item.get("patch")
    ]
    if not candidates:
        raise ValueError("No replayable structured patches found in patches.json")

    patch_item = candidates[patch_index]
    payload = patch_item.get("reverse_patch") if reverse else patch_item.get("patch")
    if not payload:
        raise ValueError("Selected patch does not contain the requested patch payload")

    patch = dict(payload)
    path_value = patch.get("path")
    if path_value:
        patch["path"] = str((repo / path_value).resolve())

    result = ApplyPatchTool().execute(patch)
    return {
        "artifact_dir": str(artifact_path),
        "repo_root": str(repo),
        "selected_tool": patch_item.get("tool_name"),
        "reverse": reverse,
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "metadata": result.metadata or {},
    }


def _split_front_matter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---\n"):
        return {}, raw

    lines = raw.splitlines()
    closing_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return {}, raw

    meta_lines = lines[1:closing_index]
    body = "\n".join(lines[closing_index + 1 :])
    metadata: dict[str, str] = {}
    for line in meta_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, body


def _coerce_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _coerce_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


def _mark_preverified_artifact(
    artifact_dir: Path,
    *,
    verification_cmd: str,
    cwd: str,
    grader: dict[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
) -> None:
    metrics_path = artifact_dir / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["preflight_verified"] = True
    metrics["verification_cmd"] = verification_cmd
    metrics["verification_cwd"] = cwd
    metrics["grader"] = grader
    metrics["grader_checks"] = checks or []
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    result_path = artifact_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["preflight_verified"] = True
    result["grader"] = grader
    result["grader_checks"] = checks or []
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _detect_code_version() -> str | None:
    probe = Path.cwd().resolve()
    for candidate in [probe, *probe.parents]:
        if not (candidate / ".git").exists():
            continue
        head = candidate / ".git" / "HEAD"
        if not head.exists():
            return None
        raw = head.read_text(encoding="utf-8").strip()
        if raw.startswith("ref: "):
            ref = raw.split(" ", 1)[1]
            ref_path = candidate / ".git" / ref
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip() or None
            return None
        return raw or None
    return None
