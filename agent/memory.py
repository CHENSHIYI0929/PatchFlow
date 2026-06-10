"""
agent/memory.py

Tiny local run memory used for later benchmark and agent heuristics.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.task import RunResult, Task


@dataclass
class MemoryHit:
    score: int
    payload: dict[str, Any]


def append_run_memory(
    log_dir: str | Path,
    task: Task,
    result: RunResult,
    *,
    lessons: list[str] | None = None,
    changed_files: list[str] | None = None,
    successful_tools: list[str] | None = None,
) -> Path:
    memory_dir = Path(log_dir) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / "run_memory.jsonl"
    payload: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task_id": task.task_id,
        "task_file": task.task_file,
        "repo_path": task.repo_path,
        "source_repo_path": task.source_repo_path,
        "task_description": task.description,
        "test_cmd": task.test_cmd,
        "status": result.status.value,
        "steps_taken": result.steps_taken,
        "failure_type": result.failure_type,
        "failure_stage": result.failure_stage,
        "failure_message": result.failure_message,
        "summary": result.summary,
        "changed_files": changed_files or [],
        "successful_tools": successful_tools or [],
        "lessons": lessons or _default_lessons(task, result),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def load_recent_memories(log_dir: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    path = Path(log_dir) / "memory" / "run_memory.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def search_memories(
    log_dir: str | Path,
    *,
    query: str,
    repo_path: str | Path | None = None,
    limit: int = 5,
) -> list[MemoryHit]:
    """Keyword-search local run memory without external dependencies."""
    terms = _terms(query)
    repo = str(Path(repo_path).resolve()) if repo_path else None
    hits: list[MemoryHit] = []
    for payload in load_recent_memories(log_dir, limit=200):
        score = _score_memory(payload, terms, repo)
        if score > 0:
            hits.append(MemoryHit(score=score, payload=payload))
    hits.sort(key=lambda item: (item.score, item.payload.get("created_at", "")), reverse=True)
    return hits[:limit]


def format_memory_hits(hits: list[MemoryHit]) -> str:
    if not hits:
        return ""
    lines = ["[LONG MEMORY] Relevant prior run memories:"]
    for hit in hits:
        payload = hit.payload
        parts = [
            f"- status={payload.get('status')}",
            f"steps={payload.get('steps_taken')}",
        ]
        failure_type = payload.get("failure_type")
        if failure_type:
            parts.append(f"failure_type={failure_type}")
        test_cmd = payload.get("test_cmd")
        if test_cmd:
            parts.append(f"test_cmd={test_cmd}")
        summary = payload.get("summary") or payload.get("task_description") or ""
        line = " ".join(parts)
        if summary:
            line += f" :: {_clip(summary, 180)}"
        lessons = payload.get("lessons") or []
        if lessons:
            line += " Lessons: " + "; ".join(_clip(str(item), 120) for item in lessons[:2])
        lines.append(line)
    return "\n".join(lines)


def _score_memory(payload: dict[str, Any], terms: set[str], repo: str | None) -> int:
    haystack = " ".join(
        str(payload.get(key, ""))
        for key in (
            "task_description",
            "summary",
            "failure_type",
            "failure_stage",
            "failure_message",
            "test_cmd",
        )
    ).lower()
    score = sum(2 for term in terms if term in haystack)
    if repo and repo in {
        payload.get("repo_path"),
        payload.get("source_repo_path"),
    }:
        score += 8
    if payload.get("status") == "success":
        score += 1
    return score


def _terms(text: str) -> set[str]:
    ignored = {
        "the", "and", "for", "with", "from", "this", "that", "fix", "bug",
        "please", "issue", "task", "test", "tests",
    }
    return {
        term.lower()
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
        if term.lower() not in ignored
    }


def _default_lessons(task: Task, result: RunResult) -> list[str]:
    lessons: list[str] = []
    if result.is_success():
        lessons.append("A similar task succeeded; prefer the recorded verification command and minimal patch strategy.")
    if result.failure_type:
        lessons.append(f"Previous failure was classified as {result.failure_type}; recover using the matching taxonomy strategy.")
    if task.test_cmd:
        lessons.append(f"Use targeted verification first: {task.test_cmd}")
    return lessons


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
