from __future__ import annotations


COUPONS = {
    "SAVE10": 0.10,
    "SAVE25": 0.25,
    "VIP5": 0.05,
}


def normalize_coupon(code: str | None) -> str | None:
    if code is None:
        return None
    cleaned = code.strip()
    if not cleaned:
        return None
    return cleaned.upper()


def discount_for_code(code: str | None) -> float:
    normalized = normalize_coupon(code)
    if normalized is None:
        return 0.0
    return COUPONS.get(normalized, 0.0)
