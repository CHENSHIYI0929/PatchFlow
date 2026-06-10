"""
agent/edit_plan.py

Edit plan parsing and validation for patch actions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
WRITE_TOOLS = {"apply_patch", "file_write"}


@dataclass
class EditPlan:
    target_files: list[str]
    change_intent: str
    expected_behavior: str
    risk_level: str = "medium"
    tests_to_run: list[str] = field(default_factory=list)
    source: str = "model"
    valid: bool = True
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_files": self.target_files,
            "change_intent": self.change_intent,
            "expected_behavior": self.expected_behavior,
            "risk_level": self.risk_level,
            "tests_to_run": self.tests_to_run,
            "source": self.source,
            "valid": self.valid,
            "errors": self.errors,
        }


def parse_or_infer_edit_plan(
    thought: str,
    tool_name: str,
    params: dict[str, Any],
    *,
    default_test_cmd: str | None = None,
) -> EditPlan:
    explicit = _parse_explicit_plan(thought)
    if explicit:
        return explicit

    path = params.get("path")
    target_files = [str(path)] if path else []
    risk = "medium"
    if tool_name == "file_write" or params.get("patch_type") == "replace_file":
        risk = "high"
    return EditPlan(
        target_files=target_files,
        change_intent=thought.strip() or f"Apply {tool_name}",
        expected_behavior="The requested task should pass its targeted verification.",
        risk_level=risk,
        tests_to_run=[default_test_cmd] if default_test_cmd else [],
        source="inferred",
    )


def validate_edit_plan(plan: EditPlan, *, exclude_paths: list[str], target_files: list[str]) -> EditPlan:
    errors = list(plan.errors)
    normalized_excludes = [Path(path).as_posix().rstrip("/") for path in exclude_paths]
    normalized_targets = {Path(path).as_posix() for path in target_files}

    if plan.risk_level not in ALLOWED_RISK_LEVELS:
        errors.append(f"Invalid risk_level: {plan.risk_level}")
    if not plan.target_files:
        errors.append("EDIT_PLAN target_files cannot be empty for write actions.")
    for path in plan.target_files:
        normalized = Path(path).as_posix()
        if any(normalized == excluded or normalized.startswith(excluded + "/") for excluded in normalized_excludes):
            errors.append(f"EDIT_PLAN targets excluded path: {path}")
    if normalized_targets:
        for path in plan.target_files:
            normalized = Path(path).as_posix()
            if not any(normalized == target or normalized.endswith("/" + target) for target in normalized_targets):
                errors.append(f"EDIT_PLAN target is outside declared target_files: {path}")

    plan.errors = errors
    plan.valid = not errors
    return plan


def format_edit_plan_for_prompt() -> str:
    return (
        "Before calling apply_patch or file_write, include an EDIT_PLAN JSON object in your thought:\n"
        'EDIT_PLAN: {"target_files":["path.py"],"change_intent":"...","expected_behavior":"...",'
        '"risk_level":"low|medium|high","tests_to_run":["pytest ..."]}'
    )


def _parse_explicit_plan(thought: str) -> EditPlan | None:
    marker = "EDIT_PLAN:"
    if marker not in thought:
        return None
    raw = thought.split(marker, 1)[1].strip()
    if "\n" in raw:
        raw = raw.splitlines()[0].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return EditPlan(
            target_files=[],
            change_intent="",
            expected_behavior="",
            source="model",
            valid=False,
            errors=["EDIT_PLAN is not valid JSON."],
        )
    return EditPlan(
        target_files=[str(item) for item in data.get("target_files", [])],
        change_intent=str(data.get("change_intent", "")),
        expected_behavior=str(data.get("expected_behavior", "")),
        risk_level=str(data.get("risk_level", "medium")),
        tests_to_run=[str(item) for item in data.get("tests_to_run", [])],
        source="model",
    )
