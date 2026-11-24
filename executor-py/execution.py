"""Delta-neutral 1:1 execution, CEX guardrails, partial-fill handling, and route unwind."""

from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

PERP_SUFFIX = ":USDT"
DEFAULT_GUARDRAILS: Dict[str, Dict[str, float]] = {
    "default": {"tick_size": 0.01, "step_size": 0.001, "min_notional": 5.0},
}


def _to_dec(val: Any) -> Decimal:
    return Decimal(str(val))


def round_price(price: float, tick_size: float, side: str = "buy") -> float:
    """Round price to exchange tick_size precision."""
    if tick_size <= 0:
        raise ValueError("tick_size must be > 0")
    p, t = _to_dec(price), _to_dec(tick_size)
    rounding = ROUND_CEILING if side.lower() == "sell" else ROUND_DOWN
    units = (p / t).to_integral_value(rounding=rounding)
    return float(units * t)


def round_qty(qty: float, step_size: float) -> float:
    """Floor quantity to exchange step_size (lot size) precision."""
    if step_size <= 0:
        raise ValueError("step_size must be > 0")
    q, s = _to_dec(qty), _to_dec(step_size)
    units = (q / s).to_integral_value(rounding=ROUND_DOWN)
    return float(units * s)


def compatible_step_size(step_a: float, step_b: float) -> float:
    """Compute a common step size that is an exact multiple of both step_a and step_b."""
    if step_a <= 0 or step_b <= 0:
        raise ValueError("step_size must be > 0")
    da, db = _to_dec(step_a), _to_dec(step_b)
    # Scale to integers to find LCM, fallback to max if clean multiple
    exp = max(-da.as_tuple().exponent, -db.as_tuple().exponent, 0)
    scale = Decimal(10) ** exp
    ia, ib = int(da * scale), int(db * scale)
    import math
    lcm_int = (ia * ib) // math.gcd(ia, ib)
    return float(Decimal(lcm_int) / scale)


def compute_1to1_qty(
    target_notional_usdt: float,
    price_a: float,
    price_b: float,
    guardrail_a: Dict[str, float],
    guardrail_b: Dict[str, float],
) -> float:
    """
    Calculate identical 1:1 base asset quantity for Leg A and Leg B satisfying
    both exchanges' step_size and min_notional guardrails.
    """
    if target_notional_usdt <= 0 or price_a <= 0 or price_b <= 0:
        raise ValueError("target_notional_usdt and prices must be > 0")

    step = compatible_step_size(
        guardrail_a.get("step_size", 0.0001),
        guardrail_b.get("step_size", 0.0001),
    )
    ref_price = max(price_a, price_b)
    qty = round_qty(target_notional_usdt / ref_price, step)
    if qty <= 0:
        raise ValueError("Calculated 1:1 quantity is zero after step_size rounding")

    validate_min_notional(qty, price_a, guardrail_a.get("min_notional", 5.0), "leg_a")
    validate_min_notional(qty, price_b, guardrail_b.get("min_notional", 5.0), "leg_b")
    return qty


def validate_min_notional(qty: float, price: float, min_notional: float, leg_name: str = "") -> float:
    notional = float(_to_dec(qty) * _to_dec(price))
    if notional < min_notional:
        raise ValueError(
            f"Notional {notional:.4f} USDT below min_notional {min_notional:.4f} USDT on {leg_name}"
        )
    return notional


def action_to_side(action: str) -> str:
    act = action.upper()
    if act in ("BUY", "BUY_LONG", "BUY_SPOT", "LONG"):
        return "buy"
    if act in ("SELL", "SELL_SHORT", "SELL_SPOT", "SHORT"):
        return "sell"
    raise ValueError(f"Unsupported leg action: {action}")


def opposite_side(side: str) -> str:
    return "sell" if side.lower() == "buy" else "buy"


class DeltaNeutralExecutor:
    """Executes 1:1 delta-neutral dual-leg routes with partial-fill & unwind protection."""

    def __init__(
        self,
        exchanges: Optional[Dict[str, Any]] = None,
        guardrails: Optional[Dict[str, Dict[str, float]]] = None,
        max_leg_b_retries: int = 2,
        notifier: Optional[Callable[[str], None]] = None,
    ):
        self.exchanges = exchanges or {}
        self.guardrails = guardrails or DEFAULT_GUARDRAILS
        self.max_leg_b_retries = max_leg_b_retries
        self.notifier = notifier
        self.dry_run_log: list[Dict[str, Any]] = []

    def get_guardrail(self, exchange: str, symbol: str = "") -> Dict[str, float]:
        key = f"{exchange}:{symbol}"
        if key in self.guardrails:
            return self.guardrails[key]
        if exchange in self.guardrails:
            return self.guardrails[exchange]
        # Try reading ccxt market metadata if present
        client = self.exchanges.get(exchange)
        if client and hasattr(client, "markets") and client.markets and symbol in client.markets:
            m = client.markets[symbol]
            limits = m.get("limits", {})
            precision = m.get("precision", {})
            is_contract = bool(m.get("contract")) or (PERP_SUFFIX in symbol)
            ct_size = float(m.get("contractSize") or 1.0) if is_contract else 1.0
            raw_step = float(precision.get("amount", 0.001) or 0.001)
            return {
                "tick_size": float(precision.get("price", 0.01) or 0.01),
                "step_size": float(_to_dec(raw_step) * _to_dec(ct_size)),
                "min_notional": float(limits.get("cost", {}).get("min", 5.0) or 5.0),
                "contract_size": ct_size,
            }
        return self.guardrails.get("default", DEFAULT_GUARDRAILS["default"])

    def _to_order_amount(self, exchange: str, symbol: str, base_qty: float) -> tuple[float, float]:
        gr = self.get_guardrail(exchange, symbol)
        ct_size = float(gr.get("contract_size", 1.0) or 1.0)
        if ct_size <= 0 or PERP_SUFFIX not in symbol:
            return base_qty, 1.0
        return float(_to_dec(base_qty) / _to_dec(ct_size)), ct_size

    def _resolve_filled_base(
        self, client: Any, symbol: str, order: Dict[str, Any], req_order_amt: float, ct_size: float
    ) -> tuple[float, str]:
        order_id = order.get("id")
        filled_raw = order.get("filled")
        status = str(order.get("status") or "").lower()
        if (filled_raw is None or status == "") and order_id and hasattr(client, "fetch_order"):
            try:
                fetched = client.fetch_order(order_id, symbol)
                if isinstance(fetched, dict):
                    filled_raw = fetched.get("filled", filled_raw)
                    status = str(fetched.get("status") or status).lower()
            except Exception as exc:
                logger.warning("fetch_order fallback failed for %s (%s): %s", order_id, symbol, exc)
        # Unknown fill state is never interpreted as a full fill; reconcile before hedging.
        filled_amt = float(filled_raw) if filled_raw is not None else 0.0
        return float(_to_dec(filled_amt) * _to_dec(ct_size)), (status or "closed")

    def check_sufficient_balance(self, exchange: str, required_usdt: float) -> bool:
        """Verify exchange has enough free USDT balance when fetch_balance is available."""
        client = self.exchanges.get(exchange)
        if not client or not hasattr(client, "fetch_balance"):
            logger.error("Cannot verify free USDT balance on %s; rejecting live execution", exchange)
            return False
        try:
            bal = client.fetch_balance()
            free_usdt = bal.get("free", {}).get("USDT")
            if free_usdt is None:
                free_usdt = bal.get("USDT", {}).get("free")
            if free_usdt is None:
                logger.error("Free USDT balance missing on %s; rejecting live execution", exchange)
                return False
            return float(free_usdt) >= required_usdt
        except Exception as exc:
            logger.warning("Failed to check balance on %s: %s", exchange, exc)
            return False

    def _place_leg_a(
        self, ex_a: str, client_a: Any, sym_a: str, side_a: str, qty: float, price_a: float
    ) -> tuple[Dict[str, Any], float, bool]:
        ord_amt_a, ct_size_a = self._to_order_amount(ex_a, sym_a, qty)
        order_a = client_a.create_order(sym_a, "limit", side_a, ord_amt_a, price_a)
        order_a_id = order_a.get("id", "ord_a")
        filled_a, status_a = self._resolve_filled_base(client_a, sym_a, order_a, ord_amt_a, ct_size_a)
        cancelled_unfilled_a = False
        if filled_a < qty or status_a in ("open", "partially_filled", "partial", "live", "new"):
            try:
                client_a.cancel_order(order_a_id, sym_a)
                cancelled_unfilled_a = True
            except Exception as exc:
                logger.warning("Failed to cancel remaining leg_a order %s: %s", order_a_id, exc)
        return order_a, filled_a, cancelled_unfilled_a

    def _leg_b_order_price(self, ord_type: str, side_b: str, sym_b: str, price_b: float) -> Optional[float]:
        if ord_type == "limit" or (side_b == "buy" and PERP_SUFFIX not in sym_b):
            return price_b
        return None

    def _fill_leg_b_with_retries(
        self, ex_b: str, client_b: Any, sym_b: str, side_b: str, target_qty_b: float, price_b: float
    ) -> tuple[float, Optional[Dict[str, Any]], Optional[str]]:
        filled_b = 0.0
        order_b = None
        last_error = None
        for attempt in range(self.max_leg_b_retries + 1):
            remaining_b = float(_to_dec(target_qty_b) - _to_dec(filled_b))
            if remaining_b <= 0:
                break
            try:
                ord_type = "limit" if attempt == 0 else "market"
                ord_price = self._leg_b_order_price(ord_type, side_b, sym_b, price_b)
                ord_amt_b, ct_size_b = self._to_order_amount(ex_b, sym_b, remaining_b)
                order_b = client_b.create_order(sym_b, ord_type, side_b, ord_amt_b, ord_price)
                step_fill, _ = self._resolve_filled_base(client_b, sym_b, order_b, ord_amt_b, ct_size_b)
                if step_fill < remaining_b and order_b.get("id"):
                    self._try_cancel(client_b, order_b["id"], sym_b)
                filled_b = float(_to_dec(filled_b) + _to_dec(step_fill))
                if filled_b >= target_qty_b:
                    break
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Leg B attempt %d failed: %s", attempt + 1, exc)
        return filled_b, order_b, last_error

    def _try_cancel(self, client: Any, order_id: str, symbol: str) -> None:
        try:
            client.cancel_order(order_id, symbol)
        except Exception:
            pass

    def execute_signal(
        self,
        signal: Dict[str, Any],
        dry_run: bool = False,
        effective_notional_usdt: Optional[float] = None,
    ) -> Dict[str, Any]:
        legs = signal["legs"]
        leg_a, leg_b = legs["leg_a"], legs["leg_b"]
        notional = float(
            effective_notional_usdt if effective_notional_usdt is not None else signal["target_notional_usdt"]
        )
        sym_a = leg_a.get("canon_symbol") or leg_a["raw_symbol"]
        sym_b = leg_b.get("canon_symbol") or leg_b["raw_symbol"]
        gr_a = self.get_guardrail(leg_a["exchange"], sym_a)
        gr_b = self.get_guardrail(leg_b["exchange"], sym_b)
        side_a, side_b = action_to_side(leg_a["action"]), action_to_side(leg_b["action"])
        if side_a == side_b:
            raise ValueError("Delta-neutral route requires opposite sides on leg_a and leg_b")

        price_a = round_price(float(leg_a["vwap_price"]), gr_a["tick_size"], side_a)
        price_b = round_price(float(leg_b["vwap_price"]), gr_b["tick_size"], side_b)
        qty = compute_1to1_qty(notional, price_a, price_b, gr_a, gr_b)

        if dry_run:
            return self._record_dry_run(signal, notional, qty, leg_a, leg_b, sym_a, sym_b, side_a, side_b, price_a, price_b)

        if not self.check_sufficient_balance(leg_a["exchange"], qty * price_a):
            return {"status": "REJECTED", "reason": "insufficient_balance_leg_a", "signal_id": signal["signal_id"], "exchange": leg_a["exchange"]}
        if not self.check_sufficient_balance(leg_b["exchange"], qty * price_b):
            return {"status": "REJECTED", "reason": "insufficient_balance_leg_b", "signal_id": signal["signal_id"], "exchange": leg_b["exchange"]}

        client_a, client_b = self.exchanges[leg_a["exchange"]], self.exchanges[leg_b["exchange"]]
        order_a, filled_a, cancelled_unfilled_a = self._place_leg_a(
            leg_a["exchange"], client_a, sym_a, side_a, qty, price_a
        )
        if filled_a <= 0:
            return {
                "status": "ABORTED_ZERO_FILL_A",
                "signal_id": signal["signal_id"],
                "qty_requested": qty,
                "qty_filled_a": 0.0,
                "qty_filled_b": 0.0,
                "cancelled_unfilled_a": cancelled_unfilled_a,
            }

        target_qty_b = round_qty(filled_a, compatible_step_size(gr_a["step_size"], gr_b["step_size"]))
        if target_qty_b <= 0:
            unwind_order = self._emergency_unwind(leg_a["exchange"], client_a, sym_a, side_a, filled_a)
            return {
                "status": "EMERGENCY_UNWOUND",
                "signal_id": signal["signal_id"],
                "reason": "filled_a_below_step_size",
                "unwound_qty_a": filled_a,
                "unwind_order": unwind_order,
            }

        filled_b, order_b, last_error = self._fill_leg_b_with_retries(
            leg_b["exchange"], client_b, sym_b, side_b, target_qty_b, price_b
        )
        unhedged_a = float(_to_dec(filled_a) - _to_dec(filled_b))
        if unhedged_a > 0:
            return self._handle_unhedged_leg_a(
                signal, client_a, sym_a, side_a, leg_a["exchange"], qty, filled_a, filled_b, unhedged_a, cancelled_unfilled_a, last_error
            )

        status = "FILLED_1TO1" if filled_a == qty else "PARTIAL_FILLED_1TO1"
        if self.notifier:
            self.notifier(f"[EXEC] {status} {signal['base_asset']} qty={filled_a}")
        return {
            "status": status,
            "signal_id": signal["signal_id"],
            "base_asset": signal["base_asset"],
            "qty": filled_a,
            "qty_requested": qty,
            "qty_filled_a": filled_a,
            "qty_filled_b": filled_b,
            "price_a": price_a,
            "price_b": price_b,
            "leg_a": {"exchange": leg_a["exchange"], "symbol": sym_a, "side": side_a, "price": price_a, "qty": filled_a},
            "leg_b": {"exchange": leg_b["exchange"], "symbol": sym_b, "side": side_b, "price": price_b, "qty": filled_b},
            "cancelled_unfilled_a": cancelled_unfilled_a,
            "order_a": order_a,
            "order_b": order_b,
        }

    def _record_dry_run(
        self, signal: Dict[str, Any], notional: float, qty: float,
        leg_a: Dict[str, Any], leg_b: Dict[str, Any], sym_a: str, sym_b: str,
        side_a: str, side_b: str, price_a: float, price_b: float,
    ) -> Dict[str, Any]:
        record = {
            "status": "DRY_RUN_Simulated",
            "signal_id": signal["signal_id"],
            "profile_id": signal["profile_id"],
            "base_asset": signal["base_asset"],
            "effective_notional_usdt": notional,
            "qty": qty,
            "leg_a": {"exchange": leg_a["exchange"], "symbol": sym_a, "side": side_a, "price": price_a, "qty": qty},
            "leg_b": {"exchange": leg_b["exchange"], "symbol": sym_b, "side": side_b, "price": price_b, "qty": qty},
            "timestamp_ms": int(time.time() * 1000),
        }
        self.dry_run_log.append(record)
        return record

    def _handle_unhedged_leg_a(
        self, signal: Dict[str, Any], client_a: Any, sym_a: str, side_a: str, ex_a: str,
        qty: float, filled_a: float, filled_b: float, unhedged_a: float,
        cancelled_unfilled_a: bool, last_error: Optional[str],
    ) -> Dict[str, Any]:
        unwind_order = self._emergency_unwind(ex_a, client_a, sym_a, side_a, unhedged_a)
        if self.notifier:
            self.notifier(f"[ALERT] Emergency unwind on {sym_a} ({ex_a}): unhedged={unhedged_a}")
        status = "EMERGENCY_UNWOUND" if filled_b == 0 else "PARTIAL_HEDGED_AND_UNWOUND"
        return {
            "status": status,
            "signal_id": signal["signal_id"],
            "qty_requested": qty,
            "qty_filled_a": filled_a,
            "qty_filled_b": filled_b,
            "unwound_qty_a": unhedged_a,
            "cancelled_unfilled_a": cancelled_unfilled_a,
            "unwind_order": unwind_order,
            "error": last_error,
        }

    def _emergency_unwind(self, exchange: str, client: Any, symbol: str, original_side: str, qty: float) -> Dict[str, Any]:
        close_side = opposite_side(original_side)
        logger.error("Emergency market unwind: %s %s qty=%s", close_side, symbol, qty)
        return self._create_close_order(exchange, client, symbol, close_side, qty)

    def _create_close_order(
        self, exchange: str, client: Any, symbol: str, close_side: str, base_qty: float, ref_price: Optional[float] = None
    ) -> Dict[str, Any]:
        ord_amt, _ = self._to_order_amount(exchange, symbol, base_qty)
        if PERP_SUFFIX in symbol:
            try:
                return client.create_order(symbol, "market", close_side, ord_amt, None, {"reduceOnly": True})
            except TypeError as exc:
                raise RuntimeError(
                    f"{exchange} client rejected reduceOnly; refusing an unsafe non-reduce-only close"
                ) from exc
        if close_side == "buy" and ref_price is not None and ref_price > 0:
            return client.create_order(symbol, "market", close_side, ord_amt, ref_price)
        return client.create_order(symbol, "market", close_side, ord_amt, None)

    def unwind_route(self, position: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """Close both legs of an active 1:1 route via market orders."""
        leg_a = position["leg_a"]
        leg_b = position["leg_b"]
        qty = float(position["qty"])
        close_side_a = opposite_side(action_to_side(leg_a.get("side") or leg_a.get("action")))
        close_side_b = opposite_side(action_to_side(leg_b.get("side") or leg_b.get("action")))
        sym_a = leg_a.get("symbol") or leg_a.get("canon_symbol") or leg_a["raw_symbol"]
        sym_b = leg_b.get("symbol") or leg_b.get("canon_symbol") or leg_b["raw_symbol"]

        if dry_run:
            res = {
                "status": "DRY_RUN_UNWOUND",
                "base_asset": position.get("base_asset"),
                "qty": qty,
                "leg_a_close_side": close_side_a,
                "leg_b_close_side": close_side_b,
            }
            self.dry_run_log.append(res)
            return res

        client_a = self.exchanges[leg_a["exchange"]]
        client_b = self.exchanges[leg_b["exchange"]]
        ref_a = float(leg_a.get("price") or leg_a.get("vwap_price") or 0.0) or None
        ref_b = float(leg_b.get("price") or leg_b.get("vwap_price") or 0.0) or None
        ord_a = self._create_close_order(leg_a["exchange"], client_a, sym_a, close_side_a, qty, ref_a)
        ord_b = self._create_close_order(leg_b["exchange"], client_b, sym_b, close_side_b, qty, ref_b)
        if self.notifier:
            self.notifier(f"[UNWIND] Closed route {position.get('base_asset', '')} qty={qty}")
        return {
            "status": "UNWOUND_1TO1",
            "base_asset": position.get("base_asset"),
            "qty": qty,
            "order_a": ord_a,
            "order_b": ord_b,
        }

    close_route = unwind_route
