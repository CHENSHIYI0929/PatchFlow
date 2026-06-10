"""Small benchmark fixture with one intentionally buggy function."""


def safe_divide(a: float, b: float) -> float:
    """Return a / b."""
    return a / b


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Clamp value into [minimum, maximum]."""
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value
