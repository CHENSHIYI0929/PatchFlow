"""
agent/memory.py

Tiny local run memory used for later benchmark and agent heuristics.
"""

from __future__ import annotations

import hashlib
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
        "task_category": task.task_category,
        "expected_failure_type": task.expected_failure_type,
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
    _append_experience_memory(
        memory_dir,
        task=task,
        result=result,
        lessons=payload["lessons"],
        changed_files=payload["changed_files"],
        successful_tools=payload["successful_tools"],
    )
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


def load_experience_memories(log_dir: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    path = Path(log_dir) / "memory" / "experience_memory.jsonl"
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


def summarize_experience_memories(log_dir: str | Path, *, limit: int = 500) -> dict[str, Any]:
    rows = load_experience_memories(log_dir, limit=limit)
    by_category: dict[str, int] = {}
    by_failure_type: dict[str, int] = {}
    top_lessons: dict[str, int] = {}
    for row in rows:
        category = str(row.get("task_category") or "unknown")
        by_category[category] = by_category.get(category, 0) + 1
        failure_type = str(row.get("expected_failure_type") or "unknown")
        by_failure_type[failure_type] = by_failure_type.get(failure_type, 0) + 1
        for lesson in row.get("lessons") or []:
            text = _normalize_lesson(str(lesson))
            if not text:
                continue
            top_lessons[text] = top_lessons.get(text, 0) + 1
    return {
        "experience_count": len(rows),
        "by_category": by_category,
        "by_failure_type": by_failure_type,
        "top_lessons": [
            {"lesson": lesson, "count": count}
            for lesson, count in sorted(top_lessons.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
    }


def search_memories(
    log_dir: str | Path,
    *,
    query: str,
    repo_path: str | Path | None = None,
    category: str | None = None,
    failure_type: str | None = None,
    limit: int = 5,
) -> list[MemoryHit]:
    """Keyword-search local run memory without external dependencies."""
    terms = _terms(query)
    repo = str(Path(repo_path).resolve()) if repo_path else None
    hits: list[MemoryHit] = []
    for payload in load_experience_memories(log_dir, limit=200):
        score = _score_memory(payload, terms, repo, category=category, failure_type=failure_type) + 3
        if score > 0:
            hits.append(MemoryHit(score=score, payload=payload))
    for payload in load_recent_memories(log_dir, limit=200):
        score = _score_memory(payload, terms, repo, category=category, failure_type=failure_type)
        if score > 0:
            hits.append(MemoryHit(score=score, payload=payload))
    hits.sort(key=lambda item: (item.score, item.payload.get("created_at", "")), reverse=True)
    return hits[:limit]


def format_memory_hits(
    hits: list[MemoryHit],
    *,
    category: str | None = None,
    failure_type: str | None = None,
) -> str:
    if not hits:
        return ""
    lines = ["[LONG MEMORY] Relevant prior run memories:"]
    success_patterns = _summarize_success_patterns(hits, category=category, failure_type=failure_type)
    if success_patterns:
        label = f"category={category}" if category else "similar tasks"
        lines.append(f"Successful patterns for {label}:")
        lines.extend(f"- {item}" for item in success_patterns[:3])
    recovery_patterns = _summarize_recovery_patterns(hits, failure_type=failure_type)
    if recovery_patterns:
        label = failure_type or "observed failures"
        lines.append(f"Recovery hints for failure_type={label}:")
        lines.extend(f"- {item}" for item in recovery_patterns[:3])
    for hit in hits:
        payload = hit.payload
        parts = [
            f"- status={payload.get('status')}",
            f"steps={payload.get('steps_taken')}",
        ]
        memory_kind = payload.get("memory_kind")
        if memory_kind:
            parts.append(f"memory={memory_kind}")
        task_category = payload.get("task_category")
        if task_category:
            parts.append(f"category={task_category}")
        observed_failure_type = payload.get("failure_type")
        expected_failure_type = payload.get("expected_failure_type")
        if observed_failure_type:
            parts.append(f"failure_type={observed_failure_type}")
        elif expected_failure_type:
            parts.append(f"expected_failure_type={expected_failure_type}")
        test_cmd = payload.get("test_cmd")
        if test_cmd:
            parts.append(f"test_cmd={test_cmd}")
        summary = payload.get("summary") or payload.get("task_description") or ""
        line = " ".join(parts)
        if summary:
            line += f" :: {_clip(summary, 180)}"
        lessons = payload.get("lessons") or []
        if lessons:
            line += " Lessons: " + "; ".join(_clip(_normalize_lesson(str(item)), 120) for item in lessons[:2])
        lines.append(line)
    return "\n".join(lines)


def _score_memory(
    payload: dict[str, Any],
    terms: set[str],
    repo: str | None,
    *,
    category: str | None = None,
    failure_type: str | None = None,
) -> int:
    haystack = " ".join(
        str(payload.get(key, ""))
        for key in (
            "task_description",
            "summary",
            "failure_type",
            "expected_failure_type",
            "task_category",
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
    if category and payload.get("task_category") == category:
        score += 10
    expected = payload.get("expected_failure_type")
    observed = payload.get("failure_type")
    if failure_type and failure_type in {expected, observed}:
        score += 6
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


def _append_experience_memory(
    memory_dir: Path,
    *,
    task: Task,
    result: RunResult,
    lessons: list[str],
    changed_files: list[str],
    successful_tools: list[str],
) -> None:
    if not result.is_success():
        return
    signature = _experience_signature(
        repo_path=task.source_repo_path or task.repo_path,
        category=task.task_category,
        expected_failure_type=task.expected_failure_type,
        test_cmd=task.test_cmd,
        lessons=lessons,
        changed_files=changed_files,
    )
    payload: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "memory_kind": "experience",
        "experience_signature": signature,
        "task_id": task.task_id,
        "task_file": task.task_file,
        "repo_path": task.repo_path,
        "source_repo_path": task.source_repo_path,
        "task_category": task.task_category,
        "expected_failure_type": task.expected_failure_type,
        "task_description": task.description,
        "test_cmd": task.test_cmd,
        "status": result.status.value,
        "steps_taken": result.steps_taken,
        "summary": result.summary,
        "lessons": lessons,
        "changed_files": changed_files,
        "successful_tools": successful_tools,
    }
    path = memory_dir / "experience_memory.jsonl"
    if _experience_exists(path, signature):
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _experience_exists(path: Path, signature: str, *, limit: int = 200) -> bool:
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines[-limit:]):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("experience_signature") == signature:
            return True
    return False


def _experience_signature(
    *,
    repo_path: str | None,
    category: str | None,
    expected_failure_type: str | None,
    test_cmd: str | None,
    lessons: list[str],
    changed_files: list[str],
) -> str:
    payload = {
        "repo_path": repo_path or "",
        "task_category": category or "",
        "expected_failure_type": expected_failure_type or "",
        "test_cmd": test_cmd or "",
        "lessons": [str(item).strip() for item in lessons[:3]],
        "changed_files": sorted(str(item) for item in changed_files),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _summarize_success_patterns(
    hits: list[MemoryHit],
    *,
    category: str | None = None,
    failure_type: str | None = None,
) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        payload = hit.payload
        if payload.get("status") != "success":
            continue
        if category and payload.get("task_category") not in {category, None}:
            continue
        if failure_type and payload.get("expected_failure_type") not in {failure_type, None}:
            continue
        for lesson in payload.get("lessons") or []:
            text = _normalize_lesson(str(lesson))
            if text and text not in seen:
                patterns.append(_clip(text, 140))
                seen.add(text)
        test_cmd = payload.get("test_cmd")
        if test_cmd:
            item = f"Prefer targeted verification first: {test_cmd}"
            if item not in seen:
                patterns.append(_clip(item, 140))
                seen.add(item)
    return patterns


def _summarize_recovery_patterns(
    hits: list[MemoryHit],
    *,
    failure_type: str | None = None,
) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        payload = hit.payload
        observed = payload.get("failure_type")
        expected = payload.get("expected_failure_type")
        if failure_type and failure_type not in {observed, expected}:
            continue
        for lesson in payload.get("lessons") or []:
            text = _normalize_lesson(str(lesson))
            if failure_type and failure_type not in text and observed != failure_type and expected != failure_type:
                continue
            if text and text not in seen:
                patterns.append(_clip(text, 140))
                seen.add(text)
        message = payload.get("failure_message")
        if message and observed:
            item = f"{observed}: {_clip(str(message), 120)}"
            if item not in seen:
                patterns.append(item)
                seen.add(item)
    return patterns


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _normalize_lesson(text: str) -> str:
    normalized = " ".join(str(text).strip().split())
    if not normalized:
        return ""
    lowered = normalized.lower()
    if lowered.startswith("use targeted verification first:"):
        return "Prefer targeted verification first:" + normalized.split(":", 1)[1]
    if lowered.startswith("use targeted verification first "):
        return normalized.replace("Use targeted verification first", "Prefer targeted verification first", 1)
    return normalized
