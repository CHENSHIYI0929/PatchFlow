"""
agent/review.py

Lightweight deterministic checks before accepting a FINISH action.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    def to_metadata(self) -> dict[str, object]:
        return {
            "findings": self.findings,
            "review_type": "finish_self_review",
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
        )

    return ReviewResult(success=True, message="Self-review passed.")
