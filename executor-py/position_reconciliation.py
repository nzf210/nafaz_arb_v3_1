"""CEX position quantity retrieval for watchdog reconciliation."""

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)
PERP_SUFFIX = ":USDT"


def leg_symbol(leg: Dict[str, Any]) -> str:
    return str(leg.get("symbol") or leg.get("canon_symbol") or leg.get("raw_symbol") or "unknown")


def _find_position_qty(client: Any, symbol: str) -> Optional[float]:
    positions = client.fetch_positions([symbol])
    if not isinstance(positions, list):
        raise ValueError("fetch_positions returned an invalid response")
    for position in positions:
        info = position.get("info") or {}
        if (position.get("symbol") or info.get("symbol")) != symbol:
            continue
        quantity = position.get("contracts")
        if quantity is None:
            quantity = position.get("amount", 0.0)
        contract_size = position.get("contractSize")
        if contract_size is None:
            contract_size = (getattr(client, "markets", {}) or {}).get(symbol, {}).get("contractSize")
        return abs(float(quantity or 0.0) * float(contract_size or 1.0))
    return None


def _fetch_positions_qty(client: Any, exchange: str, symbol: str) -> Tuple[Optional[float], Optional[str]]:
    if not hasattr(client, "fetch_positions"):
        return None, "fetch_positions unsupported for this position"
    try:
        quantity = _find_position_qty(client, symbol)
    except Exception as exc:
        logger.warning("Failed to fetch position qty for %s on %s: %s", symbol, exchange, exc)
        return None, f"fetch_positions failed: {exc}"
    return (quantity if quantity is not None else 0.0), None


def _fetch_spot_qty(client: Any, exchange: str, symbol: str) -> Tuple[Optional[float], Optional[str]]:
    if not hasattr(client, "fetch_balance"):
        return None, "no supported position or balance endpoint"
    try:
        balance = client.fetch_balance()
    except Exception as exc:
        logger.warning("Failed to fetch spot balance for %s on %s: %s", symbol, exchange, exc)
        return None, f"fetch_balance failed: {exc}"
    if not isinstance(balance, dict):
        return None, "fetch_balance returned an invalid response"
    base = symbol.split("/")[0].split("-")[0].split("_")[0]
    try:
        return abs(float(balance.get("total", {}).get(base, 0.0) or 0.0)), None
    except (TypeError, ValueError, OverflowError):
        return None, "spot balance quantity invalid"


def fetch_leg_qty(leg: Dict[str, Any], exchanges: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    if "actual_qty" in leg:
        try:
            return abs(float(leg["actual_qty"])), None
        except (TypeError, ValueError, OverflowError):
            return None, "invalid supplied actual_qty"
    exchange = str(leg.get("exchange") or "")
    client = exchanges.get(exchange)
    if not client:
        return None, f"exchange client unavailable for {exchange or 'unknown CEX'}"
    symbol = leg_symbol(leg)
    if symbol == "unknown":
        return None, "position symbol unavailable"
    if PERP_SUFFIX in symbol or (hasattr(client, "fetch_positions") and not hasattr(client, "fetch_balance")):
        return _fetch_positions_qty(client, exchange, symbol)
    return _fetch_spot_qty(client, exchange, symbol)
