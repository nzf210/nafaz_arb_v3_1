"""Per-leg close verification, flat-leg skipping, and realized PnL capture for unwind routes."""

import logging
import time
from typing import Any, Dict, Optional

from pnl import compute_realized_pnl, resolve_direction, resolve_fill_price

logger = logging.getLogger(__name__)

PERP_SUFFIX = ":USDT"
QTY_EPSILON = 1e-10


class UnwindMixin:
    """Closes both legs using exchange-verified quantities; host supplies order helpers."""

    def _position_base_qty(self, client: Any, symbol: str, position: Dict[str, Any]) -> float:
        info = position.get("info") or {}
        if (position.get("symbol") or info.get("symbol")) != symbol:
            return 0.0
        contracts = position.get("contracts")
        if contracts is None:
            contracts = position.get("amount", 0.0)
        contract_size = position.get("contractSize")
        if contract_size is None and getattr(client, "markets", None):
            contract_size = (client.markets.get(symbol) or {}).get("contractSize")
        return abs(float(contracts or 0.0) * float(contract_size or 1.0))

    def _perpetual_position_is_flat(self, client: Any, symbol: str) -> bool:
        if not hasattr(client, "fetch_positions"):
            return False
        positions = client.fetch_positions([symbol])
        if not isinstance(positions, list):
            return False
        return all(self._position_base_qty(client, symbol, pos) <= QTY_EPSILON for pos in positions)

    def _create_close_order(
        self,
        exchange: str,
        client: Any,
        symbol: str,
        close_side: str,
        base_qty: float,
        ref_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        ord_amt, _ = self._to_order_amount(exchange, symbol, base_qty)
        if PERP_SUFFIX in symbol:
            try:
                return client.create_order(symbol, "market", close_side, ord_amt, None, {"reduceOnly": True})
            except TypeError as exc:
                raise RuntimeError(
                    f"{exchange} client rejected reduceOnly; refusing an unsafe non-reduce-only close"
                ) from exc
            except Exception as exc:
                if "posside" in str(exc).lower() or "51000" in str(exc):
                    pos_side = "long" if close_side.lower() == "sell" else "short"
                    return client.create_order(
                        symbol, "market", close_side, ord_amt, None, {"reduceOnly": True, "posSide": pos_side}
                    )
                raise
        if close_side == "buy" and ref_price is not None and ref_price > 0:
            return client.create_order(symbol, "market", close_side, ord_amt, ref_price)
        return client.create_order(symbol, "market", close_side, ord_amt, None)

    def _exchange_leg_qty(self, leg: Dict[str, Any], symbol: str) -> tuple[Optional[float], Optional[str]]:
        """Exchange-truth quantity for one leg, or None when the position API failed."""
        from position_reconciliation import fetch_leg_qty

        try:
            return fetch_leg_qty(leg, self.exchanges)
        except Exception as exc:
            logger.warning("Leg quantity lookup failed for %s: %s", symbol, exc)
            return None, str(exc)

    def _resolve_close_qty(
        self, leg: Dict[str, Any], symbol: str, stored_qty: float
    ) -> tuple[float, bool, Optional[str]]:
        """Return (qty_to_close, already_flat, data_issue)."""
        actual, issue = self._exchange_leg_qty(leg, symbol)
        if actual is None:
            # Position API unavailable: close stored quantity and let fill verification decide.
            return stored_qty, False, issue or "position quantity unavailable"
        if actual <= QTY_EPSILON:
            return 0.0, True, None
        return actual, False, None

    def _close_leg_and_verify(
        self,
        leg: Dict[str, Any],
        symbol: str,
        close_side: str,
        base_qty: float,
        already_flat: bool = False,
    ) -> tuple[Optional[Dict[str, Any]], bool, str, float]:
        exchange = leg["exchange"]
        client = self.exchanges[exchange]
        if already_flat or base_qty <= QTY_EPSILON:
            return {"status": "already_flat"}, True, "already_flat", 0.0
        ref_price = float(leg.get("price") or leg.get("vwap_price") or 0.0) or None
        try:
            order = self._create_close_order(exchange, client, symbol, close_side, base_qty, ref_price)
        except Exception as exc:
            return None, False, f"close_error:{exc}", 0.0
        order_amount, contract_size = self._to_order_amount(exchange, symbol, base_qty)
        filled_base, order_status = self._resolve_filled_base(client, symbol, order, order_amount, contract_size)
        if filled_base + QTY_EPSILON < base_qty:
            return order, False, f"close_fill_incomplete:{filled_base}/{base_qty}", filled_base
        if PERP_SUFFIX in symbol and not self._perpetual_position_is_flat(client, symbol):
            return order, False, "exchange_position_not_flat", filled_base
        return order, True, order_status, filled_base

    def _safe_close_leg(
        self, leg: Dict[str, Any], symbol: str, stored_qty: float
    ) -> tuple[Optional[Dict[str, Any]], bool, str, float]:
        direction = resolve_direction(leg)
        if direction is None:
            return None, False, "close_error:unresolved leg side", 0.0
        close_side = "sell" if direction > 0 else "buy"
        qty, already_flat, issue = self._resolve_close_qty(leg, symbol, stored_qty)
        order, verified, detail, filled = self._close_leg_and_verify(
            leg, symbol, close_side, qty, already_flat=already_flat
        )
        if order and issue:
            detail = f"{detail};{issue}"
        return order, verified, detail, filled

    def unwind_route(self, position: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """Close both legs (skipping legs already flat) and capture realized PnL."""
        leg_a, leg_b = position["leg_a"], position["leg_b"]
        qty = float(position["qty"])
        sym_a = leg_a.get("symbol") or leg_a.get("canon_symbol") or leg_a["raw_symbol"]
        sym_b = leg_b.get("symbol") or leg_b.get("canon_symbol") or leg_b["raw_symbol"]

        if dry_run:
            res = {
                "status": "DRY_RUN_UNWOUND",
                "base_asset": position.get("base_asset"),
                "qty": qty,
                "leg_a_close_side": "sell" if resolve_direction(leg_a) == 1 else "buy",
                "leg_b_close_side": "sell" if resolve_direction(leg_b) == 1 else "buy",
            }
            self.dry_run_log.append(res)
            return res

        order_a, verified_a, detail_a, closed_a = self._safe_close_leg(leg_a, sym_a, qty)
        order_b, verified_b, detail_b, closed_b = self._safe_close_leg(leg_b, sym_b, qty)
        verified = verified_a and verified_b
        result: Dict[str, Any] = {
            "status": "UNWOUND_VERIFIED" if verified else "UNWIND_PENDING",
            "base_asset": position.get("base_asset"),
            "qty": qty,
            "order_a": order_a,
            "order_b": order_b,
            "verified_flat": verified,
            "leg_a_verified": verified_a,
            "leg_b_verified": verified_b,
            "leg_a_detail": detail_a,
            "leg_b_detail": detail_b,
            "closed_qty_a": closed_a,
            "closed_qty_b": closed_b,
        }
        if verified:
            self._refresh_close_prices(order_a, order_b, position)
        self._store_pnl(position, result)
        if self.notifier:
            level = "UNWIND VERIFIED" if verified else "UNWIND RECONCILIATION REQUIRED"
            self.notifier(f"[{level}] {position.get('base_asset', '')} qty={qty}")
        return result

    def _store_pnl(self, position: Dict[str, Any], result: Dict[str, Any]) -> None:
        pnl = compute_realized_pnl(position, result)
        result.update(
            {
                "realized_pnl_usdt": pnl["realized_pnl_usdt"],
                "realized_pnl_net_usdt": pnl["net_pnl_usdt"],
                "fees_usdt": pnl["fees_usdt"],
                "pnl_status": pnl["pnl_status"],
                "fee_status": pnl["fee_status"],
                "pnl_legs": pnl["legs"],
            }
        )

    def _refresh_close_prices(
        self,
        order_a: Optional[Dict[str, Any]],
        order_b: Optional[Dict[str, Any]],
        position: Dict[str, Any],
    ) -> None:
        """Backfill missing average prices and fees from fetch_order/ticker/trades before PnL is persisted."""
        for order, leg_key in ((order_a, "leg_a"), (order_b, "leg_b")):
            if not isinstance(order, dict) or order.get("status") == "already_flat":
                continue
            leg = position.get(leg_key) or {}
            symbol = leg.get("symbol") or leg.get("canon_symbol") or leg.get("raw_symbol")
            client = self.exchanges.get(leg.get("exchange") or "")
            if not order.get("average") and not order.get("price"):
                price = resolve_fill_price(client, symbol or "", order)
                if price:
                    order["average"] = price
            if order.get("fee") is None and client and hasattr(client, "fetch_my_trades") and symbol:
                try:
                    order_id = order.get("id")
                    since = order.get("timestamp") or (int(time.time() * 1000) - 15_000)
                    trades = client.fetch_my_trades(symbol, since=since)
                    match_trades = [t for t in trades if str(t.get("order")) == str(order_id)] if order_id else trades
                    if match_trades:
                        total_fee = sum(float(t["fee"]["cost"]) for t in match_trades if isinstance(t.get("fee"), dict) and t["fee"].get("cost") is not None)
                        order["fee"] = {"currency": "USDT", "cost": total_fee}
                except Exception as exc:
                    logger.debug("Failed to backfill exit fee for %s: %s", symbol, exc)

    close_route = unwind_route