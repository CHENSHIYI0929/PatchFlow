"""
agent/hybrid_retrieval.py

Lightweight deterministic retrieval that combines failure signals, target files,
symbols, and keyword matching without embeddings or LLM calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.failure_analyzer import FailureAnalysis


_CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h"}


@dataclass
class RetrievalCandidate:
    path: str
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "score": round(self.score, 3), "reasons": self.reasons}


def retrieve_candidates(
    repo_path: str | Path,
    *,
    task_description: str,
    analysis: FailureAnalysis | None = None,
    target_files: list[str] | None = None,
    limit: int = 8,
) -> list[RetrievalCandidate]:
    repo = Path(repo_path).resolve()
    scores: dict[str, RetrievalCandidate] = {}

    def add(path: str, score: float, reason: str) -> None:
        rel = _normalize_path(repo, path)
        item = scores.setdefault(rel, RetrievalCandidate(path=rel, score=0.0, reasons=[]))
        item.score += score
        if reason not in item.reasons:
            item.reasons.append(reason)

    for path in target_files or []:
        add(path, 12.0, "target_file")

    if analysis:
        for path in analysis.traceback_files:
            add(path, 10.0, "traceback_file")
        for test in analysis.failed_tests:
            file_part = test.split("::", 1)[0]
            add(file_part, 4.0, "failed_test_file")

    terms = _terms(task_description)
    if analysis:
        terms.update(_terms(" ".join(analysis.failed_tests)))
        terms.update(_terms(" ".join(analysis.suspect_symbols)))
        if analysis.error_type:
            terms.add(analysis.error_type.lower())

    for path in _iter_code_files(repo):
        rel = str(path.relative_to(repo))
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        hits = sum(1 for term in terms if term and term in text)
        if hits:
            add(rel, float(hits), f"keyword_hits={hits}")
        if analysis:
            symbol_hits = sum(1 for symbol in analysis.suspect_symbols if symbol and symbol.lower() in text)
            if symbol_hits:
                add(rel, symbol_hits * 3.0, f"symbol_hits={symbol_hits}")

    return sorted(scores.values(), key=lambda item: item.score, reverse=True)[:limit]


def format_candidates_for_prompt(candidates: list[RetrievalCandidate]) -> str:
    if not candidates:
        return "[HYBRID RETRIEVAL]\nNo strong candidate files found."
    lines = ["[HYBRID RETRIEVAL] Candidate files to inspect next:"]
    for item in candidates[:8]:
        lines.append(f"- {item.path} score={item.score:.1f} reasons={', '.join(item.reasons)}")
    return "\n".join(lines)


def _iter_code_files(repo: Path):
    for path in repo.rglob("*"):
        if not path.is_file() or path.suffix not in _CODE_SUFFIXES:
            continue
        parts = set(path.parts)
        if {"__pycache__", ".git", ".pytest_cache", "logs"} & parts:
            continue
        yield path


def _normalize_path(repo: Path, path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return str(candidate.resolve().relative_to(repo))
        except ValueError:
            return str(candidate)
    return str(candidate)


def _terms(text: str) -> set[str]:
    ignored = {"the", "and", "for", "with", "from", "this", "that", "fix", "bug", "test", "tests"}
    return {
        term.lower()
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
        if term.lower() not in ignored
    }
