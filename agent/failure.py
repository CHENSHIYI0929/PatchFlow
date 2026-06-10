"""
agent/failure.py

Canonical failure taxonomy shared by agent, grader, CLI, and reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


FAILURE_TYPE_VERIFICATION_FAILED = "verification_failed"
FAILURE_TYPE_TOOL_FAILURE = "tool_failure"
FAILURE_TYPE_PATCH_CONFLICT = "patch_conflict"
FAILURE_TYPE_TIMEOUT = "timeout"
FAILURE_TYPE_LLM_ERROR = "llm_error"
FAILURE_TYPE_WORKSPACE_ERROR = "workspace_error"
FAILURE_TYPE_UNKNOWN = "unknown"

FAILURE_STAGE_PREVERIFY = "preverify"
FAILURE_STAGE_AGENT_LOOP = "agent_loop"
FAILURE_STAGE_GRADING = "grading"
FAILURE_STAGE_ARTIFACT_EXPORT = "artifact_export"


@dataclass(frozen=True)
class FailureInfo:
    reason: str
    failure_type: str
    failure_stage: str
    failure_message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "failure_type": self.failure_type,
            "failure_stage": self.failure_stage,
            "failure_message": self.failure_message,
        }


def failure(reason: str, *, failure_type: str, failure_stage: str, failure_message: str | None = None) -> FailureInfo:
    return FailureInfo(
        reason=reason,
        failure_type=failure_type,
        failure_stage=failure_stage,
        failure_message=failure_message or reason,
    )


def classify_timeout_text(text: str) -> bool:
    return "timed out" in text.lower() or "timeout" in text.lower()


def classify_exception_failure(exc: Exception) -> str:
    return FAILURE_TYPE_TIMEOUT if classify_timeout_text(str(exc)) else FAILURE_TYPE_LLM_ERROR


def classify_command_failure(returncode: int | None, output: str) -> str:
    text = output.lower()
    if returncode == -1 and classify_timeout_text(text):
        return FAILURE_TYPE_TIMEOUT
    if "conflict" in text:
        return FAILURE_TYPE_PATCH_CONFLICT
    return FAILURE_TYPE_VERIFICATION_FAILED


def normalize_agent_loop_failure(reason: str) -> str:
    if classify_timeout_text(reason):
        return FAILURE_TYPE_TIMEOUT
    return FAILURE_TYPE_UNKNOWN


def infer_failure_from_event_dicts(
    events: list[dict[str, Any]],
    *,
    default_stage: str,
) -> FailureInfo | None:
    for event in reversed(events):
        if event.get("event_type") != "observation":
            continue
        observation = (event.get("payload") or {}).get("observation") or {}
        if observation.get("status") == "success":
            continue
        metadata = observation.get("metadata") or {}
        message = str(observation.get("error") or observation.get("output") or "Tool failed").strip()
        failure_type = metadata.get("failure_type")
        if not failure_type:
            if "conflict" in message.lower():
                failure_type = FAILURE_TYPE_PATCH_CONFLICT
            elif classify_timeout_text(message):
                failure_type = FAILURE_TYPE_TIMEOUT
            else:
                failure_type = FAILURE_TYPE_TOOL_FAILURE
        return failure(
            message,
            failure_type=failure_type,
            failure_stage=default_stage,
            failure_message=message,
        )
    return None
