from report import build_report
from scores import average_score, parse_score_line


LINES = [
    "Math: 95",
    "science: 85",
    "English: 90",
]


def test_parse_score_line_trims_whitespace() -> None:
    assert parse_score_line(" math : 95 ") == ("math", 95.0)


def test_parse_score_line_normalizes_subject_case() -> None:
    assert parse_score_line("Science: 85") == ("science", 85.0)


def test_average_score() -> None:
    assert average_score(LINES) == 90.0


def test_build_report_renders_lowercase_subjects() -> None:
    report = build_report("Alex", LINES)
    assert "- math: 95.0" in report
    assert "- science: 85.0" in report
    assert "- english: 90.0" in report


def test_build_report_includes_average() -> None:
    report = build_report("Alex", LINES)
    assert "Average: 90.0" in report

