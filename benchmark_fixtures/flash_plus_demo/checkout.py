from __future__ import annotations

from discounts import discount_for_code
from tax import tax_rate_for_state


def subtotal(items: list[dict[str, float]]) -> float:
    total = 0.0
    for item in items:
        total += item["price"] * item["quantity"]
    return round(total, 2)


def checkout_total(
    items: list[dict[str, float]],
    state: str | None,
    coupon_code: str | None = None,
) -> float:
    base = subtotal(items)
    discount_rate = discount_for_code(coupon_code)
    tax_rate = tax_rate_for_state(state)
    taxed = base + (base * tax_rate)
    discounted = taxed - (base * discount_rate)
    return round(discounted, 2)
