"""
context/compression.py

Deterministic context compression for long agent runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from llm.base import LLMMessage


_PATH_RE = re.compile(r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|cpp|c|h|md|yaml|yml|json|toml)")


@dataclass
class CompressionSummary:
    """Compact state preserved when old conversation turns leave the window."""

    message_count: int = 0
    actions: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    failure_trajectories: list[str] = field(default_factory=list)
    reflections: list[str] = field(default_factory=list)
    memory_patterns: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        if self.message_count == 0:
            return ""
        sections = [
            f"[COMPRESSED CONTEXT] Summary of {self.message_count} older messages.",
        ]
        if self.actions:
            sections.append("Recent older actions: " + "; ".join(self.actions[-5:]))
        if self.observations:
            sections.append("Important older observations: " + "; ".join(self.observations[-5:]))
        if self.failures:
            sections.append("Known failures: " + "; ".join(self.failures[-5:]))
        if self.failure_trajectories:
            sections.append("Failure trajectory to preserve: " + "; ".join(self.failure_trajectories[-5:]))
        if self.memory_patterns:
            sections.append("Prior successful patterns: " + "; ".join(self.memory_patterns[-4:]))
        if self.files:
            sections.append("Files mentioned: " + ", ".join(self.files[-12:]))
        if self.reflections:
            sections.append("Prior reflections: " + "; ".join(self.reflections[-3:]))
        return "\n".join(sections)

    def to_dict(self) -> dict:
        return {
            "message_count": self.message_count,
            "actions": self.actions,
            "observations": self.observations,
            "files": self.files,
            "failures": self.failures,
            "failure_trajectories": self.failure_trajectories,
            "reflections": self.reflections,
            "memory_patterns": self.memory_patterns,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "CompressionSummary":
        if not data:
            return cls()
        return cls(
            message_count=int(data.get("message_count", 0)),
            actions=list(data.get("actions") or []),
            observations=list(data.get("observations") or []),
            files=list(data.get("files") or []),
            failures=list(data.get("failures") or []),
            failure_trajectories=list(data.get("failure_trajectories") or []),
            reflections=list(data.get("reflections") or []),
            memory_patterns=list(data.get("memory_patterns") or []),
        )


class ContextCompressor:
    """Small deterministic summarizer used before dropping old messages."""

    def __init__(self, max_items: int = 20) -> None:
        self._max_items = max_items

    def update(self, summary: CompressionSummary, messages: list[LLMMessage]) -> CompressionSummary:
        for message in messages:
            content = message.content or ""
            summary.message_count += 1
            self._collect_files(summary, content)
            if message.role == "assistant":
                self._collect_action(summary, content)
            else:
                self._collect_user_context(summary, content)
        self._cap(summary)
        return summary

    def _collect_action(self, summary: CompressionSummary, content: str) -> None:
        action = self._line_after_prefix(content, "Action:")
        thought = self._line_after_prefix(content, "Thought:")
        if action:
            detail = action
            if thought:
                detail = f"{action} ({self._clip(thought, 80)})"
            summary.actions.append(detail)

    def _collect_user_context(self, summary: CompressionSummary, content: str) -> None:
        lowered = content.lower()
        if content.startswith("[LONG MEMORY]"):
            self._collect_long_memory(summary, content)
            return
        if "failed" in lowered or "error" in lowered or "traceback" in lowered:
            signal = self._clip(self._failure_signal_line(content), 140)
            summary.failures.append(signal)
            summary.failure_trajectories.append(signal)
        elif content.startswith("[REFLECTION]") or content.startswith("[RECOVERY]"):
            summary.reflections.append(self._clip(self._first_signal_line(content), 140))
            summary.failure_trajectories.append(self._clip(self._first_signal_line(content), 140))
        elif "Observation" in content or "Output:" in content:
            summary.observations.append(self._clip(self._first_signal_line(content), 140))

    def _collect_files(self, summary: CompressionSummary, content: str) -> None:
        seen = set(summary.files)
        for path in _PATH_RE.findall(content):
            if path not in seen:
                summary.files.append(path)
                seen.add(path)

    def _cap(self, summary: CompressionSummary) -> None:
        for field_name in (
            "actions",
            "observations",
            "files",
            "failures",
            "failure_trajectories",
            "reflections",
            "memory_patterns",
        ):
            values = getattr(summary, field_name)
            if len(values) > self._max_items:
                setattr(summary, field_name, values[-self._max_items:])

    def _collect_long_memory(self, summary: CompressionSummary, content: str) -> None:
        current_section: str | None = None
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("Successful patterns for"):
                summary.memory_patterns.append(self._clip(stripped, 160))
                current_section = "success"
            elif stripped.startswith("Recovery hints for"):
                summary.failure_trajectories.append(self._clip(stripped, 160))
                current_section = "recovery"
            elif "Lessons:" in stripped:
                lessons = stripped.split("Lessons:", 1)[1].split(";")
                for lesson in lessons[:2]:
                    lesson_text = lesson.strip()
                    if lesson_text:
                        summary.memory_patterns.append(self._clip(lesson_text, 140))
            elif stripped.startswith("- "):
                item = stripped[2:].strip()
                if not item or item.startswith("status="):
                    continue
                if current_section == "recovery":
                    summary.failure_trajectories.append(self._clip(item, 140))
                else:
                    summary.memory_patterns.append(self._clip(item, 140))

    def _line_after_prefix(self, content: str, prefix: str) -> str | None:
        for line in content.splitlines():
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return None

    def _first_signal_line(self, content: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return content.strip()

    def _failure_signal_line(self, content: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if "failed" in lowered or "traceback" in lowered or "assert" in lowered or "error:" in lowered:
                return stripped
        return self._first_signal_line(content)

    def _clip(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."


def compressed_summary_message(summary: CompressionSummary) -> LLMMessage | None:
    text = summary.to_text()
    if not text:
        return None
    return LLMMessage(role="user", content=text)


def summary_to_json(summary: CompressionSummary) -> str:
    return json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True)
