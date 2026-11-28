"""Position reconciliation: drift classification, stale/lopsided auto-repair, verified close finalization."""

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from pnl import compute_realized_pnl
from position_reconciliation import fetch_leg_qty, leg_symbol

logger = logging.getLogger(__name__)

REDIS_POSITIONS_KEY = "core:positions"
POSITION_ALERT_INTERVAL_MS = 300_000
QTY_EPSILON = 1e-10


class ReconcileMixin:
    """Drift detection and repair for RiskWatchdog (host supplies db, notifier, exchanges)."""

    def _load_positions(self, provided: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        if provided is not None:
            return provided
        raw = self.redis.get(REDIS_POSITIONS_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw:
            return json.loads(raw)
        if self.db_store is not None and hasattr(self.db_store, "load_active_positions"):
            return self.db_store.load_active_positions()
        return []

    def reconcile_positions(
        self, expected_positions: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Compare expected positions with exchange truth, auto-repairing broken legs."""
        positions = self._load_positions(expected_positions)
        drifts: List[Dict[str, Any]] = []
        unavailable: List[Dict[str, Any]] = []
        repaired: List[Dict[str, Any]] = []
        for pos in positions:
            drift, data_issue, repair = self._reconcile_position(pos)
            if drift:
                drifts.append(drift)
            if data_issue:
                unavailable.append(data_issue)
            if repair:
                repaired.append(repair)

        if drifts:
            status = "DRIFT_DETECTED"
        elif unavailable:
            status = "DATA_UNAVAILABLE"
        elif repaired:
            status = "REPAIRED"
        else:
            status = "OK"
        self.maybe_clear_auto_kill_switch(healthy=status in ("OK", "REPAIRED"))
        return {"status": status, "drifts": drifts, "unavailable": unavailable, "repaired": repaired}

    def _reconcile_position(
        self, pos: Dict[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if getattr(self, "dry_run", False):
            # Simulated positions have no exchange truth; reconciliation would false-positive.
            return None, None, None
        base_asset = pos.get("base_asset", "UNKNOWN")
        try:
            expected_qty = float(pos.get("qty", 0.0))
        except (TypeError, ValueError, OverflowError):
            expected_qty = math.nan
        actual_a, issue_a = fetch_leg_qty(pos.get("leg_a", {}), self.exchanges)
        actual_b, issue_b = fetch_leg_qty(pos.get("leg_b", {}), self.exchanges)
        data_issues = self._build_position_data_issues(
            pos, base_asset, expected_qty, actual_a, actual_b, issue_a, issue_b
        )
        if data_issues:
            if self._should_report_position_alert(base_asset, "POSITION_DATA_UNAVAILABLE", data_issues):
                self._report_position_data_issues(data_issues)
            return None, data_issues, None
        actual_a = float(actual_a)
        actual_b = float(actual_b)
        if self._is_stale_flat(expected_qty, actual_a, actual_b):
            return None, None, self._finalize_stale_flat(pos, expected_qty)
        leg_diff = abs(actual_a - actual_b)
        drift = max(leg_diff, abs(actual_a - expected_qty), abs(actual_b - expected_qty))
        if drift <= self.drift_tolerance:
            self._position_alerts.pop(base_asset, None)
            return None, None, None
        repaired = self._repair_single_leg(pos, actual_a, actual_b)
        if repaired:
            return None, None, repaired
        drift_info = self._position_drift_details(pos, expected_qty, actual_a, actual_b, leg_diff)
        if self._should_report_position_alert(base_asset, "POSITION_DRIFT", drift_info):
            self._report_position_drift(drift_info)
        return drift_info, None, None

    def _is_stale_flat(self, expected_qty: float, actual_a: float, actual_b: float) -> bool:
        return expected_qty > QTY_EPSILON and actual_a <= QTY_EPSILON and actual_b <= QTY_EPSILON

    def _repair_single_leg(
        self, pos: Dict[str, Any], actual_a: float, actual_b: float
    ) -> Optional[Dict[str, Any]]:
        """Flatten a position where only one leg still holds exposure (no more lopsided closes)."""
        flat_a, flat_b = actual_a <= QTY_EPSILON, actual_b <= QTY_EPSILON
        if flat_a == flat_b or self.unwind_callback is None:
            return None
        base_asset = pos.get("base_asset", "UNKNOWN")
        repaired_pos = dict(pos)
        repaired_pos["qty"] = max(actual_a, actual_b)
        self._record_risk(
            "POSITION_SINGLE_LEG_REPAIR", "WARNING",
            {"base_asset": base_asset, "actual_qty_a": actual_a, "actual_qty_b": actual_b},
        )
        self.notifier.send(
            f"[DRIFT REPAIR] {base_asset}: single-leg exposure detected "
            f"(a={actual_a}, b={actual_b}); flattening remaining leg"
        )
        try:
            result = self.unwind_callback(repaired_pos)
        except Exception as exc:
            logger.warning("Single-leg repair failed for %s: %s", base_asset, exc)
            return None
        if str(result.get("status", "")).startswith("DRY_RUN"):
            return None
        if not (result.get("status") == "UNWOUND_VERIFIED" and result.get("verified_flat") is True):
            self._record_risk(
                "POSITION_SINGLE_LEG_REPAIR_FAILED", "CRITICAL",
                {"base_asset": base_asset, "unwind_result": result},
            )
            return None
        self._finalize_position(pos, result, "drift_single_leg_auto_repair")
        return {"base_asset": base_asset, "action": "flatten_single_leg", "unwind": result}

    def _finalize_stale_flat(self, pos: Dict[str, Any], expected_qty: float) -> Dict[str, Any]:
        """Close a stored position whose legs are already flat on both exchanges."""
        stale_result = {
            "status": "STALE_FLAT",
            "verified_flat": True,
            "order_a": None,
            "order_b": None,
            "closed_qty_a": 0.0,
            "closed_qty_b": 0.0,
            "expected_qty": expected_qty,
        }
        base_asset = pos.get("base_asset", "UNKNOWN")
        self._record_risk(
            "POSITION_STALE_FLAT_CLOSED", "WARNING",
            {"base_asset": base_asset, "expected_qty": expected_qty},
        )
        self.notifier.send(
            f"[STALE POSITION CLOSED] {base_asset}: both legs already flat on exchange "
            f"(expected={expected_qty}); bookkeeping closed"
        )
        self._finalize_position(pos, stale_result, "stale_flat_auto_closed")
        return {"base_asset": base_asset, "action": "stale_flat_close", "expected_qty": expected_qty}

    def _finalize_position(
        self, position: Dict[str, Any], unwind_result: Dict[str, Any], close_reason: str
    ) -> bool:
        """Persist realized PnL, close the position row, and notify Telegram."""
        now_ms = int(time.time() * 1000)
        if "funding_usdt" not in unwind_result or unwind_result.get("funding_usdt") is None:
            from pnl import calculate_position_funding

            funding_val, _ = calculate_position_funding(
                position, exchanges=getattr(self, "exchanges", None), curr_ms=now_ms, closed_at_ms=now_ms
            )
            if funding_val is not None:
                unwind_result["funding_usdt"] = funding_val

        pnl = compute_realized_pnl(position, unwind_result, fallback_estimated_fees=True)
        unwind_result.update(
            {
                "realized_pnl_usdt": pnl["realized_pnl_usdt"],
                "realized_pnl_net_usdt": pnl["net_pnl_usdt"],
                "fees_usdt": pnl["fees_usdt"],
                "entry_fees_usdt": pnl.get("entry_fees_usdt"),
                "exit_fees_usdt": pnl.get("exit_fees_usdt"),
                "funding_usdt": pnl.get("funding_usdt"),
                "pnl_status": pnl["pnl_status"],
                "fee_status": pnl["fee_status"],
                "pnl_legs": pnl["legs"],
                "close_reason": close_reason,
            }
        )
        base_asset = position.get("base_asset", "UNKNOWN")
        recorded_pnl = pnl["net_pnl_usdt"] if pnl["net_pnl_usdt"] is not None else pnl["realized_pnl_usdt"]
        self.mark_position_closed(base_asset, recorded_pnl, close_reason)
        if self.db_store is not None and hasattr(self.db_store, "record_closed_trade"):
            try:
                self.db_store.record_closed_trade(position, unwind_result, pnl, close_reason)
            except Exception as exc:
                logger.warning("Failed to persist closed trade for %s: %s", base_asset, exc)
        if hasattr(self.notifier, "notify_position_closed"):
            try:
                self.notifier.notify_position_closed(unwind_result, base_asset)
            except Exception as exc:
                logger.warning("Telegram notify_position_closed failed: %s", exc)
        return True

    def _position_drift_details(
        self,
        pos: Dict[str, Any],
        expected_qty: float,
        actual_a: float,
        actual_b: float,
        leg_diff: float,
    ) -> Dict[str, Any]:
        leg_a, leg_b = pos["leg_a"], pos["leg_b"]
        return {
            "base_asset": pos.get("base_asset", "UNKNOWN"),
            "expected_qty": expected_qty,
            "actual_qty_a": actual_a,
            "actual_qty_b": actual_b,
            "leg_drift": leg_diff,
            "leg_a_exchange": leg_a.get("exchange", "unknown"),
            "leg_a_symbol": leg_symbol(leg_a),
            "leg_b_exchange": leg_b.get("exchange", "unknown"),
            "leg_b_symbol": leg_symbol(leg_b),
        }

    def _report_position_drift(self, details: Dict[str, Any]) -> None:
        self._record_risk("POSITION_DRIFT", "WARNING", details)
        self.notifier.send(
            f"[DRIFT] {details['base_asset']}: {details['leg_a_exchange']} "
            f"{details['leg_a_symbol']}={details['actual_qty_a']}, "
            f"{details['leg_b_exchange']} {details['leg_b_symbol']}={details['actual_qty_b']}; "
            f"expected={details['expected_qty']}, leg_drift={details['leg_drift']}"
        )

    def _should_report_position_alert(
        self, base_asset: str, event_type: str, details: Dict[str, Any]
    ) -> bool:
        now_ms = int(time.time() * 1000)
        fingerprint = json.dumps({"event_type": event_type, "details": details}, sort_keys=True)
        previous = self._position_alerts.get(base_asset)
        if previous and previous[0] == fingerprint and now_ms - previous[1] < POSITION_ALERT_INTERVAL_MS:
            return False
        self._position_alerts[base_asset] = (fingerprint, now_ms)
        return True

    def _build_position_data_issues(
        self,
        pos: Dict[str, Any],
        base_asset: str,
        expected_qty: float,
        actual_a: Optional[float],
        actual_b: Optional[float],
        issue_a: Optional[str],
        issue_b: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        issues = []
        if not math.isfinite(expected_qty):
            issues.append({"leg": "position", "issue": "stored expected quantity invalid"})
        for key, actual, issue in (("leg_a", actual_a, issue_a), ("leg_b", actual_b, issue_b)):
            if issue or actual is None or not math.isfinite(actual):
                leg = pos.get(key, {})
                issues.append(
                    {
                        "leg": key,
                        "exchange": leg.get("exchange", "unknown"),
                        "symbol": leg_symbol(leg),
                        "issue": issue or "actual quantity invalid",
                    }
                )
        return {"base_asset": base_asset, "issues": issues} if issues else None

    def _report_position_data_issues(self, details: Dict[str, Any]) -> None:
        self._record_risk("POSITION_DATA_UNAVAILABLE", "WARNING", details)
        description = "; ".join(
            f"{issue['leg']} {issue.get('exchange', '')} {issue.get('symbol', '')}: {issue['issue']}"
            for issue in details["issues"]
        )
        self.notifier.send(f"[DRIFT DATA UNAVAILABLE] {details['base_asset']}: {description}")