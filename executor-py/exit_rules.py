"""Pure calculations used by the executor's position exit rules."""

import math
from typing import Any, Dict, Optional

from execution import action_to_side

BASIS_PROFILE_ID = "basis_convergence_fast"
DEFAULT_PROFILE_EXIT_SETTINGS = {
    "funding_carry_core": {
        "target_hold_days": 7.0,
        "funding_flip_confirm_samples": 3,
        "funding_flip_confirm_window_ms": 300_000,
    },
    BASIS_PROFILE_ID: {
        "target_hold_days": 1.0,
        "basis_target_spread": 0.001,
        "basis_adverse_stop_spread": 0.005,
        "funding_flip_confirm_samples": 3,
        "funding_flip_confirm_window_ms": 300_000,
    },
}


def _price(leg: Dict[str, Any], override: Optional[float] = None) -> Optional[float]:
    raw_price = override
    if raw_price is None:
        raw_price = leg.get("entry_avg_price")
    if raw_price is None:
        raw_price = leg.get("price") or leg.get("vwap_price")
    try:
        value = float(raw_price)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def calculate_basis_spread(
    position: Dict[str, Any], leg_prices: Optional[Dict[str, float]] = None
) -> Optional[float]:
    """Return short-minus-long basis relative to the long price."""
    leg_a, leg_b = position.get("leg_a", {}), position.get("leg_b", {})
    try:
        side_a = action_to_side(str(leg_a.get("side") or leg_a.get("action") or ""))
        side_b = action_to_side(str(leg_b.get("side") or leg_b.get("action") or ""))
    except ValueError:
        return None
    if side_a == side_b:
        return None

    prices = leg_prices or {}
    price_a = _price(leg_a, prices.get("leg_a"))
    price_b = _price(leg_b, prices.get("leg_b"))
    if price_a is None or price_b is None:
        return None
    long_price, short_price = (price_a, price_b) if side_a == "buy" else (price_b, price_a)
    return (short_price - long_price) / long_price


def basis_exit_reason(
    entry_basis: float,
    current_basis: float,
    target_spread: float,
    adverse_stop_spread: float,
) -> Optional[str]:
    if not all(
        math.isfinite(value)
        for value in (entry_basis, current_basis, target_spread, adverse_stop_spread)
    ):
        return None
    if target_spread < 0 or adverse_stop_spread <= 0:
        return None
    if entry_basis <= target_spread:
        return f"basis_target_already_reached(entry={entry_basis:.6f}, target={target_spread:.6f})"
    if current_basis <= target_spread:
        return f"basis_target_reached(current={current_basis:.6f}, target={target_spread:.6f})"
    if current_basis - entry_basis >= adverse_stop_spread:
        return (
            f"basis_adverse_stop(entry={entry_basis:.6f}, current={current_basis:.6f}, "
            f"move={adverse_stop_spread:.6f})"
        )
    return None
