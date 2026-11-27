"""Exit condition evaluation and exchange market-data retrieval."""

import logging
import math
from typing import Any, Dict, List, Optional

from exit_rules import BASIS_PROFILE_ID, basis_exit_reason, calculate_basis_spread

logger = logging.getLogger(__name__)
PERP_SUFFIX = ":USDT"
INVALID_BASIS_THRESHOLDS = "basis exit thresholds invalid"
INVALID_HOLD_SETTING = "hold duration setting invalid"


def _symbol(leg: Dict[str, Any]) -> str:
    return str(leg.get("symbol") or leg.get("canon_symbol") or leg.get("raw_symbol") or "")


def fetch_leg_funding_24h(leg: Dict[str, Any], exchanges: Dict[str, Any]) -> Optional[float]:
    symbol = _symbol(leg)
    if PERP_SUFFIX not in symbol:
        return 0.0
    try:
        interval_hours = max(int(leg.get("interval_hours") or 8), 1)
    except (TypeError, ValueError, OverflowError):
        return None
    if leg.get("current_funding_rate") is not None:
        try:
            return float(leg["current_funding_rate"]) * (24.0 / interval_hours)
        except (TypeError, ValueError, OverflowError):
            return None
    exchange = leg.get("exchange", "")
    client = exchanges.get(exchange)
    if not client or not hasattr(client, "fetch_funding_rate"):
        return None
    try:
        rate = client.fetch_funding_rate(symbol)
        if not isinstance(rate, dict) or rate.get("fundingRate") is None:
            return None
        live_interval = int(rate.get("intervalHours") or interval_hours)
        return float(rate["fundingRate"]) * (24.0 / max(live_interval, 1))
    except Exception as exc:
        logger.warning("Failed to fetch funding rate for %s on %s: %s", symbol, exchange, exc)
        return None


def fetch_leg_marks(
    position: Dict[str, Any], exchanges: Dict[str, Any]
) -> tuple[Optional[Dict[str, float]], Optional[str]]:
    """Current mark price per leg, used for exit profitability gating."""
    prices: Dict[str, float] = {}
    for leg_key in ("leg_a", "leg_b"):
        leg = position.get(leg_key, {})
        exchange = leg.get("exchange", "")
        symbol = _symbol(leg)
        client = exchanges.get(exchange)
        if not client or not hasattr(client, "fetch_ticker"):
            return None, f"ticker unavailable for {exchange} {symbol}"
        try:
            ticker = client.fetch_ticker(symbol)
        except Exception as exc:
            logger.warning("Failed to fetch exit ticker for %s on %s: %s", symbol, exchange, exc)
            return None, f"ticker fetch failed for {exchange} {symbol}: {exc}"
        if not isinstance(ticker, dict):
            return None, f"ticker response invalid for {exchange} {symbol}"
        price = ticker.get("mark") or ticker.get("last") or ticker.get("close")
        try:
            prices[leg_key] = float(price)
        except (TypeError, ValueError, OverflowError):
            return None, f"ticker price missing for {exchange} {symbol}"
    return prices, None


def fetch_current_basis(
    position: Dict[str, Any], exchanges: Dict[str, Any]
) -> tuple[Optional[float], Optional[str]]:
    prices, issue = fetch_leg_marks(position, exchanges)
    if issue or prices is None:
        return None, issue
    basis = calculate_basis_spread(position, prices)
    return (basis, None) if basis is not None else (None, "basis inputs are invalid")


def _hold_exit(
    position: Dict[str, Any], now_ms: int, settings: Dict[str, Any]
) -> tuple[Optional[str], Optional[str]]:
    try:
        opened_ms = int(position.get("opened_at_ms") or 0)
        max_hold_days = float(settings.get("target_hold_days", 3.0))
    except (TypeError, ValueError, OverflowError):
        return None, INVALID_HOLD_SETTING
    if not math.isfinite(max_hold_days) or max_hold_days < 0:
        return None, INVALID_HOLD_SETTING
    if opened_ms > 0 and max_hold_days > 0:
        try:
            hold_ms = int(max_hold_days * 86_400_000)
            emergency_days = float(settings.get("emergency_max_hold_days", max(max_hold_days * 2.0, 14.0)))
            emergency_ms = int(emergency_days * 86_400_000)
        except (OverflowError, TypeError, ValueError):
            return None, INVALID_HOLD_SETTING
        if now_ms - opened_ms >= emergency_ms:
            return "emergency_hold_exceeded", None
        if now_ms - opened_ms >= hold_ms:
            return "target_hold_days_reached", None
    return None, None



def _basis_exit(
    position: Dict[str, Any], settings: Dict[str, Any], exchanges: Dict[str, Any]
) -> tuple[Optional[str], Optional[str]]:
    entry_basis = calculate_basis_spread(position)
    if entry_basis is None:
        return None, "entry basis unavailable"
    current_basis, issue = fetch_current_basis(position, exchanges)
    if issue:
        return None, issue
    if current_basis is None:
        return None, "basis inputs are invalid"
    try:
        target = float(settings.get("basis_target_spread", 0.001))
        adverse_stop = float(settings.get("basis_adverse_stop_spread", 0.005))
    except (TypeError, ValueError, OverflowError):
        return None, INVALID_BASIS_THRESHOLDS
    if not math.isfinite(target) or not math.isfinite(adverse_stop):
        return None, INVALID_BASIS_THRESHOLDS
    if target < 0 or adverse_stop <= 0:
        return None, INVALID_BASIS_THRESHOLDS
    return basis_exit_reason(entry_basis, current_basis, target, adverse_stop), None


def _funding_exit(
    position: Dict[str, Any], exchanges: Dict[str, Any]
) -> tuple[Optional[str], Optional[str]]:
    short_24h = fetch_leg_funding_24h(position.get("leg_b", {}), exchanges)
    long_24h = fetch_leg_funding_24h(position.get("leg_a", {}), exchanges)
    if short_24h is None or long_24h is None:
        if short_24h is None and long_24h is None:
            missing_leg = "both legs"
        elif short_24h is None:
            missing_leg = "short leg"
        else:
            missing_leg = "long leg"
        return None, f"funding rate unavailable for {missing_leg}"
    net_24h = short_24h - long_24h
    if not math.isfinite(net_24h):
        return None, "funding rate is invalid"
    if net_24h < 0:
        return f"funding_flipped(net_24h={net_24h:.6f})", None
    return None, None


def evaluate_position_exit(
    position: Dict[str, Any],
    now_ms: int,
    settings: Dict[str, Any],
    exchanges: Dict[str, Any],
) -> tuple[Optional[str], List[str]]:
    reason, issue = _hold_exit(position, now_ms, settings)
    if reason:
        return reason, []
    issues = [issue] if issue else []
    if position.get("profile_id") == BASIS_PROFILE_ID:
        reason, issue = _basis_exit(position, settings, exchanges)
        if issue:
            issues.append(issue)
        if reason:
            return reason, issues
    reason, issue = _funding_exit(position, exchanges)
    if issue:
        issues.append(issue)
    return reason, issues
