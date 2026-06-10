"""Helpers for parsing and averaging student scores."""

from __future__ import annotations


def parse_score_line(line: str) -> tuple[str, float]:
    """Parse a line like 'math: 95' into a subject and a score."""
    subject, raw_score = line.split(":", 1)
    return subject.strip(), float(raw_score.strip())


def average_score(lines: list[str]) -> float:
    """Return the arithmetic mean of a list of score lines."""
    total = 0.0
    for line in lines:
        _, score = parse_score_line(line)
        total += score
    return total / len(lines)
