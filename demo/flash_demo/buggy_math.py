"""Small demo module with one intentionally buggy function."""


def safe_divide(a: float, b: float) -> float | None:
    """Return a / b, or None when b is zero."""
    if b == 0:
        return None
    return a / b


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Clamp value into [minimum, maximum]."""
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value

