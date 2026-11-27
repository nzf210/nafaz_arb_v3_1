"""Realized PnL resolution for closed delta-neutral positions (price-only, auditable)."""

import logging
import math
import time
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


def _non_negative(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None



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


def _convert_fee_to_usdt(
    cost: Optional[float],
    currency: Optional[str],
    fill_price: Optional[float] = None,
    symbol: Optional[str] = None,
) -> Optional[float]:
    if cost is None:
        return None
    cur = str(currency or "USDT").upper()
    if cur in ("USDT", "USD"):
        return cost
    base = symbol.split("/")[0].upper() if symbol and "/" in symbol else None
    if base and cur == base and fill_price and fill_price > 0:
        return cost * fill_price
    logger.warning("Unconvertible fee currency '%s' (cost=%s); cannot assume USDT", cur, cost)
    return None


def order_fee_usdt(
    order: Optional[Dict[str, Any]],
    fill_price: Optional[float] = None,
    symbol: Optional[str] = None,
) -> Optional[float]:
    """Extract a fee; return None when the order payload does not establish one."""
    if not isinstance(order, dict):
        return None
    price = fill_price or order_price(order)
    sym = symbol or order.get("symbol")
    fee = order.get("fee")
    if isinstance(fee, dict):
        cost = _non_negative(fee.get("cost"))
        cur = fee.get("currency")
        converted = _convert_fee_to_usdt(cost, cur, price, sym)
        if converted is not None:
            return converted
    total = 0.0
    found = False
    for entry in order.get("fees") or []:
        if isinstance(entry, dict):
            entry_cost = _non_negative(entry.get("cost"))
            entry_cur = entry.get("currency")
            converted = _convert_fee_to_usdt(entry_cost, entry_cur, price, sym)
            if converted is not None:
                found = True
                total += converted
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
    exit_fee = order_fee_usdt(order, fill_price=exit_price, symbol=leg.get("symbol"))
    entry_fee = leg.get("entry_fee_usdt")
    entry_fee_value = _non_negative(entry_fee)
    if entry_fee_value is not None:
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
    return realized - fees + float(funding_usdt or 0.0)


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


DEFAULT_TAKER_FEE_RATES = {
    "okx": 0.0005,
    "mexc": 0.0002,
    "bybit": 0.00055,
    "default": 0.0006,
}


def estimate_net_exit_pnl(
    position: Dict[str, Any],
    prices: Dict[str, float],
    exchanges: Optional[Dict[str, Any]] = None,
    default_fee_rates: Optional[Dict[str, float]] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Estimate net exit PnL for an open delta-neutral position.

    Includes:
    - Gross price PnL from entry prices to current mark prices for both legs.
    - Entry fees (exact if recorded >= 0, otherwise estimated from fee rates).
    - Exit fees (estimated from current mark prices and taker fee rates).
    - Funding cash flow (estimated based on elapsed settlement intervals and funding rates).

    Returns a dict with:
      gross_pnl_usdt: Optional[float]
      entry_fees_usdt: Optional[float]
      exit_fees_usdt: Optional[float]
      fees_usdt: Optional[float]
      funding_usdt: Optional[float]
      net_pnl_usdt: Optional[float]
      data_status: "complete" | "partial" | "unavailable"
      legs: Dict[str, Dict[str, Any]]
    """
    fee_rates = dict(DEFAULT_TAKER_FEE_RATES)
    if default_fee_rates:
        fee_rates.update(default_fee_rates)

    qty = _positive(position.get("qty"))
    if not prices or qty is None or qty <= QTY_EPSILON:
        return {
            "gross_pnl_usdt": None,
            "entry_fees_usdt": None,
            "exit_fees_usdt": None,
            "fees_usdt": None,
            "funding_usdt": None,
            "net_pnl_usdt": None,
            "data_status": "unavailable",
            "legs": {},
        }

    curr_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    opened_ms = int(position.get("opened_at_ms") or 0)
    elapsed_ms = max(0, curr_ms - opened_ms) if opened_ms > 0 else 0

    gross_total = 0.0
    entry_fees_total = 0.0
    exit_fees_total = 0.0
    has_recorded_funding = position.get("funding_usdt") is not None
    funding_total = float(position["funding_usdt"]) if has_recorded_funding else 0.0
    all_funding_available = True if has_recorded_funding else True
    all_entry_fees_exact = True
    priced_legs = 0
    legs_summary: Dict[str, Any] = {}

    for leg_key in ("leg_a", "leg_b"):
        leg = position.get(leg_key) or {}
        direction = resolve_direction(leg)
        entry_price = leg_entry_price(leg)
        symbol = str(leg.get("symbol") or leg.get("canon_symbol") or leg.get("raw_symbol") or "")
        mark_price = _positive(prices.get(leg_key))
        if mark_price is None and symbol:
            mark_price = _positive(prices.get(symbol))
        ex_name = str(leg.get("exchange", "")).lower()
        taker_fee_rate = fee_rates.get(ex_name, fee_rates["default"])

        if direction is None or entry_price is None or mark_price is None:
            legs_summary[leg_key] = {"status": "unpriced"}
            continue

        priced_legs += 1
        leg_gross = (mark_price - entry_price) * qty * direction
        gross_total += leg_gross

        entry_fee_val = _non_negative(leg.get("entry_fee_usdt"))
        if entry_fee_val is None and "entry_fee" in leg:
            entry_fee_val = _convert_fee_to_usdt(
                _non_negative(leg.get("entry_fee")),
                leg.get("entry_fee_currency"),
                entry_price,
                symbol,
            )
        if entry_fee_val is not None:
            leg_entry_fee = entry_fee_val
        else:
            all_entry_fees_exact = False
            leg_entry_fee = entry_price * qty * taker_fee_rate
        entry_fees_total += leg_entry_fee

        leg_exit_fee = mark_price * qty * taker_fee_rate
        exit_fees_total += leg_exit_fee

        leg_funding = 0.0
        if not has_recorded_funding and ":USDT" in symbol:
            try:
                interval_hours = max(int(leg.get("interval_hours") or 8), 1)
            except (TypeError, ValueError, OverflowError):
                interval_hours = 8

            funding_rate = None
            if leg.get("current_funding_rate") is not None:
                try:
                    funding_rate = float(leg["current_funding_rate"])
                except (TypeError, ValueError):
                    pass
            if funding_rate is None and exchanges and ex_name in exchanges:
                client = exchanges[ex_name]
                if hasattr(client, "fetch_funding_rate"):
                    try:
                        fr_res = client.fetch_funding_rate(symbol)
                        if isinstance(fr_res, dict) and fr_res.get("fundingRate") is not None:
                            funding_rate = float(fr_res["fundingRate"])
                    except Exception as exc:
                        logger.debug("Failed to fetch funding rate for %s: %s", symbol, exc)

            settlement_ms = interval_hours * 3600_000
            settlements = int(elapsed_ms // settlement_ms)
            if settlements == 0:
                leg_funding = 0.0
            elif funding_rate is not None:
                cash_dir = -1.0 if direction == 1.0 else 1.0
                leg_funding = mark_price * qty * funding_rate * cash_dir * max(settlements, 0)
            else:
                all_funding_available = False
            funding_total += leg_funding

        legs_summary[leg_key] = {
            "entry_price": entry_price,
            "mark_price": mark_price,
            "gross_pnl_usdt": leg_gross,
            "entry_fee_usdt": leg_entry_fee,
            "exit_fee_usdt": leg_exit_fee,
            "funding_usdt": leg_funding,
        }

    if priced_legs < 2:
        return {
            "gross_pnl_usdt": None,
            "entry_fees_usdt": None,
            "exit_fees_usdt": None,
            "fees_usdt": None,
            "funding_usdt": None,
            "net_pnl_usdt": None,
            "data_status": "unavailable",
            "legs": legs_summary,
        }

    total_fees = entry_fees_total + exit_fees_total
    net_pnl = gross_total - total_fees + funding_total

    if all_entry_fees_exact and all_funding_available:
        status = "complete"
    else:
        status = "partial"

    return {
        "gross_pnl_usdt": gross_total,
        "entry_fees_usdt": entry_fees_total,
        "exit_fees_usdt": exit_fees_total,
        "fees_usdt": total_fees,
        "funding_usdt": funding_total,
        "net_pnl_usdt": net_pnl,
        "data_status": status,
        "legs": legs_summary,
    }

