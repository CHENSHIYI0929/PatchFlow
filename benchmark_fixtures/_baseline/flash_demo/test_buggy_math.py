from buggy_math import clamp, safe_divide


def test_safe_divide_normal_case() -> None:
    assert safe_divide(10, 2) == 5


def test_safe_divide_by_zero_returns_none() -> None:
    assert safe_divide(10, 0) is None


def test_clamp_bounds() -> None:
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
