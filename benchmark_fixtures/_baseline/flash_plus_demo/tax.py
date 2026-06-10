from __future__ import annotations


STATE_TAX_RATES = {
    "CA": 0.0825,
    "NY": 0.04,
    "TX": 0.0625,
}


def tax_rate_for_state(state: str | None) -> float:
    if state is None:
        return 0.0
    normalized = state.strip().upper()
    if not normalized:
        return 0.0
    return STATE_TAX_RATES.get(normalized, 0.0)
