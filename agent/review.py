"""
agent/review.py

Lightweight deterministic checks before accepting a FINISH action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_BLOCKING_MARKERS = (
    "<<<<<<<",
    "=======",
    ">>>>>>>",
    "pdb.set_trace(",
    "breakpoint()",
)


@dataclass
class ReviewResult:
    success: bool
    message: str
    findings: list[str] = field(default_factory=list)
    risk_level: str = "low"

    def to_metadata(self) -> dict[str, object]:
        return {
            "findings": self.findings,
            "review_type": "finish_self_review",
            "risk_level": self.risk_level,
        }


def review_patch_before_finish(patch: str | None) -> ReviewResult:
    if not patch:
        return ReviewResult(success=True, message="No diff to review.")

    findings: list[str] = []
    for marker in _BLOCKING_MARKERS:
        if marker in patch:
            findings.append(f"Diff contains unresolved marker or debug hook: {marker}")

    if findings:
        return ReviewResult(
            success=False,
            message="Self-review found blocking issues in the final diff.",
            findings=findings,
            risk_level="high",
        )

    return ReviewResult(success=True, message="Self-review passed.")


def review_patch_metadata(
    metadata: dict[str, Any],
    *,
    exclude_paths: list[str] | None = None,
    allow_test_edits: bool = False,
) -> ReviewResult:
    patch = metadata.get("patch") or {}
    path = str(patch.get("path") or metadata.get("path") or "")
    findings: list[str] = []
    risk = "low"

    text_parts = [
        str(patch.get("content") or ""),
        str(patch.get("replace") or ""),
        str(patch.get("search") or ""),
    ]
    patch_text = "\n".join(text_parts)
    for marker in _BLOCKING_MARKERS:
        if marker in patch_text:
            findings.append(f"Patch contains unresolved marker or debug hook: {marker}")
            risk = "high"

    normalized = Path(path).as_posix()
    for excluded in exclude_paths or []:
        excluded_norm = Path(excluded).as_posix().rstrip("/")
        if normalized == excluded_norm or normalized.startswith(excluded_norm + "/"):
            findings.append(f"Patch modifies excluded path: {path}")
            risk = "high"

    if not allow_test_edits and _looks_like_test_path(normalized):
        findings.append(f"Patch modifies a test file: {path}")
        risk = "high"

    stats = metadata.get("stats") or {}
    if patch.get("patch_type") == "replace_file":
        line_count = int(metadata.get("line_count", 0) or 0)
        if line_count > 300:
            findings.append(f"Large full-file replacement: {line_count} lines")
            risk = "high"
    if stats.get("occurrences_replaced", 0) and int(stats.get("occurrences_replaced", 0)) > 10:
        findings.append("Patch replaced more than 10 occurrences.")
        risk = _max_risk(risk, "medium")

    if findings:
        return ReviewResult(
            success=risk != "high",
            message="Patch review found risk signals.",
            findings=findings,
            risk_level=risk,
        )
    return ReviewResult(success=True, message="Patch review passed.", risk_level=risk)


def _looks_like_test_path(path: str) -> bool:
    name = Path(path).name
    return path.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")


def _max_risk(left: str, right: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    return left if order[left] >= order[right] else right
