from checkout import checkout_total, subtotal
from discounts import discount_for_code, normalize_coupon
from tax import tax_rate_for_state


ITEMS = [{"price": 20.0, "quantity": 2}, {"price": 5.0, "quantity": 1}]


def test_subtotal() -> None:
    assert subtotal(ITEMS) == 45.0


def test_tax_lookup_is_case_insensitive() -> None:
    assert tax_rate_for_state(" ca ") == 0.0825


def test_coupon_normalization_keeps_empty_as_none() -> None:
    assert normalize_coupon("   ") is None


def test_coupon_lookup_accepts_lowercase_and_whitespace() -> None:
    assert discount_for_code(" save10 ") == 0.10


def test_checkout_applies_discount_before_tax() -> None:
    assert checkout_total(ITEMS, "CA", "SAVE10") == 43.84
