"""Realized PnL resolution for closed delta-neutral positions (price-only, auditable)."""

import logging
import math
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BUY_ACTIONS = {"BUY", "BUY_LONG", "BUY_SPOT", "LONG"}
SELL_ACTIONS = {"SELL", "SELL_SHORT", "SELL_SPOT", "SHORT"}
QTY_EPSILON = 1e-10


def _positive(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def resolve_direction(leg: Dict[str, Any]) -> Optional[float]:
    """Return +1 for long legs and -1 for short legs."""
    raw = str(leg.get("side") or leg.get("action") or "").upper()
    if raw in BUY_ACTIONS:
        return 1.0
    if raw in SELL_ACTIONS:
        return -1.0
    return None


def order_price(order: Optional[Dict[str, Any]]) -> Optional[float]:
    """Best available fill price inside a ccxt order payload."""
    if not isinstance(order, dict):
        return None
    for key in ("average", "price"):
        price = _positive(order.get(key))
        if price:
            return price
    filled = _positive(order.get("filled"))
    cost = _positive(order.get("cost"))
    if filled and cost:
        return cost / filled
    return None


def resolve_fill_price(
    client: Optional[Any], symbol: str, order: Optional[Dict[str, Any]]
) -> Optional[float]:
    """Resolve a fill price, falling back to fetch_order then the live ticker."""
    price = order_price(order)
    if price:
        return price
    order_id = (order or {}).get("id")
    if client is not None and order_id and hasattr(client, "fetch_order"):
        try:
            price = order_price(client.fetch_order(order_id, symbol))
            if price:
                return price
        except Exception as exc:
            logger.warning("fetch_order price fallback failed for %s (%s): %s", order_id, symbol, exc)
    if client is not None and hasattr(client, "fetch_ticker"):
        try:
            ticker = client.fetch_ticker(symbol) or {}
            return _positive(ticker.get("last") or ticker.get("close") or ticker.get("mark"))
        except Exception as exc:
            logger.warning("ticker price fallback failed for %s: %s", symbol, exc)
    return None


def leg_entry_price(leg: Dict[str, Any]) -> Optional[float]:
    for key in ("entry_avg_price", "price", "vwap_price"):
        price = _positive(leg.get(key))
        if price:
            return price
    return None


def order_fee_usdt(order: Optional[Dict[str, Any]]) -> Optional[float]:
    """Extract a fee; return None when the order payload does not establish one."""
    if not isinstance(order, dict):
        return None
    fee = order.get("fee")
    cost = _positive((fee or {}).get("cost")) if isinstance(fee, dict) else None
    if cost:
        return cost
    total = 0.0
    found = False
    for entry in order.get("fees") or []:
        if isinstance(entry, dict):
            entry_cost = _positive(entry.get("cost"))
            if entry_cost is not None:
                found = True
                total += entry_cost
    return total if found else None


def leg_closed_qty(position: Dict[str, Any], unwind: Dict[str, Any], suffix: str) -> float:
    qty = _positive(unwind.get(f"closed_qty_{suffix}"))
    if qty:
        return qty
    return _positive(position.get("qty")) or 0.0


def _leg_pnl(leg: Dict[str, Any], order: Optional[Dict[str, Any]], qty: float) -> tuple[Optional[float], float, float, Optional[float]]:
    direction = resolve_direction(leg)
    entry = leg_entry_price(leg)
    exit_price = order_price(order)
    exit_fee = order_fee_usdt(order)
    entry_fee = leg.get("entry_fee_usdt")
    try:
        entry_fee_value = float(entry_fee) if entry_fee is not None else None
    except (TypeError, ValueError, OverflowError):
        entry_fee_value = None
    if entry_fee_value is not None and math.isfinite(entry_fee_value) and entry_fee_value >= 0:
        fee = exit_fee
        if fee is not None:
            fee += entry_fee_value
    else:
        fee = None
    if direction is None or entry is None or exit_price is None or qty <= QTY_EPSILON:
        return None, entry, exit_price, fee
    return (exit_price - entry) * qty * direction, entry, exit_price, fee


def compute_realized_pnl(position: Dict[str, Any], unwind: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute gross realized price PnL (fees excluded) plus fee total from verified closes.

    Returns a summary dict; `realized_pnl_usdt` is None only when no leg produced a
    computable value, so closed trades never silently report an empty PnL.
    """
    funding_usdt, funding_data_available = resolve_funding_usdt(unwind)
    legs, gross, priced_legs, fees, fee_data_available = resolve_leg_pnl(position, unwind)
    realized = gross if priced_legs else None
    net = compute_net_pnl(realized, fees, funding_usdt, fee_data_available, funding_data_available)
    pnl_status = determine_pnl_status(priced_legs, fee_data_available, funding_data_available)
    return {
        "realized_pnl_usdt": realized,
        "fees_usdt": fees if fee_data_available else None,
        "funding_usdt": funding_usdt if funding_data_available else None,
        "net_pnl_usdt": net,
        "pnl_status": pnl_status,
        "fee_status": "complete" if fee_data_available else "unavailable",
        "legs": legs,
    }


def resolve_funding_usdt(unwind: Dict[str, Any]) -> tuple[Optional[float], bool]:
    if "funding_usdt" not in unwind:
        return None, False
    try:
        funding_usdt = float(unwind.get("funding_usdt"))
    except (TypeError, ValueError, OverflowError):
        return None, False
    if not math.isfinite(funding_usdt):
        return None, False
    return funding_usdt, True


def resolve_leg_pnl(
    position: Dict[str, Any], unwind: Dict[str, Any]
) -> tuple[Dict[str, Any], float, int, float, bool]:
    gross = 0.0
    fees = 0.0
    priced_legs = 0
    fee_data_available = True
    legs: Dict[str, Any] = {}
    for leg_key, order_key, suffix in (("leg_a", "order_a", "a"), ("leg_b", "order_b", "b")):
        leg = position.get(leg_key) or {}
        order = unwind.get(order_key)
        qty = leg_closed_qty(position, unwind, suffix)
        leg_pnl, entry, exit_price, fee = _leg_pnl(leg, order, qty)
        if fee is None:
            fee_data_available = False
        if fee is not None:
            fees += fee
        if leg_pnl is not None:
            gross += leg_pnl
            priced_legs += 1
        legs[leg_key] = {
            "qty": qty,
            "entry_price": entry,
            "exit_price": exit_price,
            "realized_pnl_usdt": leg_pnl,
            "fee_usdt": fee,
        }
    return legs, gross, priced_legs, fees, fee_data_available


def compute_net_pnl(
    realized: Optional[float],
    fees: float,
    funding_usdt: Optional[float],
    fee_available: bool,
    funding_available: bool,
) -> Optional[float]:
    if realized is None or not fee_available or not funding_available:
        return None
    return realized - fees - float(funding_usdt or 0.0)


def determine_pnl_status(
    priced_legs: int, fee_available: bool, funding_available: bool
) -> str:
    if priced_legs == 2:
        status = "complete"
    elif priced_legs == 1:
        status = "partial"
    else:
        status = "unavailable"
    if status != "unavailable" and not fee_available:
        return "fee_unavailable"
    if status == "complete" and not funding_available:
        return "funding_unavailable"
    return status

def estimate_price_pnl(position: Dict[str, Any], prices: Dict[str, float]) -> Optional[float]:
    """Gross price PnL if both legs were closed now at the supplied mark prices."""
    if not prices:
        return None
    total = 0.0
    priced = 0
    for leg_key in ("leg_a", "leg_b"):
        leg = position.get(leg_key) or {}
        direction = resolve_direction(leg)
        entry = leg_entry_price(leg)
        mark = _positive(prices.get(leg_key))
        qty = _positive(leg.get("qty")) or _positive(position.get("qty")) or 0.0
        if direction is None or entry is None or mark is None or qty <= QTY_EPSILON:
            continue
        total += (mark - entry) * qty * direction
        priced += 1
    return total if priced == 2 else None
