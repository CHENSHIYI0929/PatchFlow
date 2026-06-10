"""Build a small markdown report from score lines."""

from __future__ import annotations

from scores import average_score, parse_score_line


def build_report(name: str, lines: list[str]) -> str:
    """Return a markdown report for the student's scores."""
    parsed = [parse_score_line(line) for line in lines]
    average = average_score(lines)

    rows = "\n".join(f"- {subject}: {score:.1f}" for subject, score in parsed)
    return (
        f"# Report for {name}\n\n"
        f"Average: {average:.1f}\n\n"
        f"Subjects:\n{rows}\n"
    )
