"""
agent/failure_analyzer.py

Deterministic failure analysis for test output, tracebacks, and tool errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent.task import Observation


_TRACE_FILE_RE = re.compile(r'File "([^"]+)", line \d+')
_PYTEST_FAILED_RE = re.compile(r"FAILED\s+([^\s]+)")
_ERROR_TYPE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure))\b(?::\s*(.*))?")
_SYMBOL_PATTERNS = (
    re.compile(r"NameError: name ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"AttributeError: .*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"FAILED [\w./-]+::([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"in ([A-Za-z_][A-Za-z0-9_]*)\n"),
)


@dataclass
class FailureAnalysis:
    failed_tests: list[str] = field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    traceback_files: list[str] = field(default_factory=list)
    suspect_symbols: list[str] = field(default_factory=list)
    summary: str = "No structured failure signal found."

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_tests": self.failed_tests,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "traceback_files": self.traceback_files,
            "suspect_symbols": self.suspect_symbols,
            "summary": self.summary,
        }


def analyze_failure(observation: Observation) -> FailureAnalysis:
    text = "\n".join(part for part in [observation.output, observation.error or ""] if part)
    failed_tests = _dedupe(_PYTEST_FAILED_RE.findall(text))
    traceback_files = _dedupe(_TRACE_FILE_RE.findall(text))

    error_type = None
    error_message = None
    for match in _ERROR_TYPE_RE.finditer(text):
        error_type = match.group(1)
        error_message = (match.group(2) or "").strip() or None
        break

    suspect_symbols: list[str] = []
    for pattern in _SYMBOL_PATTERNS:
        suspect_symbols.extend(pattern.findall(text))
    suspect_symbols = _dedupe(suspect_symbols)

    signals: list[str] = []
    if failed_tests:
        signals.append(f"failed_tests={', '.join(failed_tests[:3])}")
    if error_type:
        signals.append(f"error_type={error_type}")
    if traceback_files:
        signals.append(f"traceback_files={', '.join(traceback_files[-3:])}")
    if suspect_symbols:
        signals.append(f"suspect_symbols={', '.join(suspect_symbols[:5])}")

    return FailureAnalysis(
        failed_tests=failed_tests,
        error_type=error_type,
        error_message=error_message,
        traceback_files=traceback_files,
        suspect_symbols=suspect_symbols,
        summary="; ".join(signals) if signals else "No structured failure signal found.",
    )


def format_failure_analysis_for_prompt(analysis: FailureAnalysis) -> str:
    return (
        "[FAILURE ANALYSIS]\n"
        f"Summary: {analysis.summary}\n"
        f"Failed tests: {', '.join(analysis.failed_tests) or 'none'}\n"
        f"Error: {analysis.error_type or 'unknown'}"
        f"{(': ' + analysis.error_message) if analysis.error_message else ''}\n"
        f"Traceback files: {', '.join(analysis.traceback_files) or 'none'}\n"
        f"Suspect symbols: {', '.join(analysis.suspect_symbols) or 'none'}"
    )


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result
