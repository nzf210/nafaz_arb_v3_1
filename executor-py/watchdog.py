"""Risk watchdog: position/balance reconciliation, margin ratio & ADL monitoring, kill-switch, Telegram alerts."""

import json
import logging
import math
import os
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from exit_monitor import evaluate_position_exit
from exit_rules import (
    DEFAULT_PROFILE_EXIT_SETTINGS,
    calculate_realized_price_pnl,
)
from position_reconciliation import fetch_leg_qty, leg_symbol

logger = logging.getLogger(__name__)

MARGIN_WARNING_THRESHOLD = 0.60
MARGIN_CRITICAL_THRESHOLD = 0.75
ADL_HIGH_RISK_THRESHOLD = 4
EXIT_DATA_ALERT_INTERVAL_MS = 300_000
POSITION_ALERT_INTERVAL_MS = 300_000
REDIS_POSITIONS_KEY = "core:positions"
PERP_SUFFIX = ":USDT"


class TelegramNotifier:
    """Structured Telegram notifier with offline outbox fallback for dry-run / testing."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        dry_run: bool = True,
    ):
        self.bot_token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.dry_run = dry_run
        self.outbox: List[str] = []

    def send(self, message: str) -> bool:
        self.outbox.append(message)
        logger.info("Telegram alert: %s", message)
        if not self.bot_token or not self.chat_id:
            return True
        prefix = "[SIMULASI] " if self.dry_run and not message.startswith("[") else ""
        text = f"{prefix}{message}"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            logger.warning("Telegram HTTP send failed: %s", exc)
            return False


class RiskWatchdog:
    """Monitors position drift, margin usage, and ADL ranks across exchanges."""

    def __init__(
        self,
        redis_client: Any,
        exchanges: Optional[Dict[str, Any]] = None,
        notifier: Optional[TelegramNotifier] = None,
        unwind_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        drift_tolerance: float = 1e-6,
        margin_warning_threshold: float = MARGIN_WARNING_THRESHOLD,
        margin_critical_threshold: float = MARGIN_CRITICAL_THRESHOLD,
        adl_threshold: int = ADL_HIGH_RISK_THRESHOLD,
        db_store: Optional[Any] = None,
    ):
        self.redis = redis_client
        self.exchanges = exchanges or {}
        self.notifier = notifier or TelegramNotifier(dry_run=True)
        self.unwind_callback = unwind_callback
        self.drift_tolerance = drift_tolerance
        self.margin_warning_threshold = margin_warning_threshold
        self.margin_critical_threshold = margin_critical_threshold
        self.adl_threshold = adl_threshold
        self.db_store = db_store
        self._exit_data_alerted_at: Dict[str, int] = {}
        self._position_alerts: Dict[str, tuple[str, int]] = {}

    def _load_exit_profile_settings(self) -> Dict[str, Dict[str, Any]]:
        settings = {key: dict(value) for key, value in DEFAULT_PROFILE_EXIT_SETTINGS.items()}
        if self.db_store is None or not hasattr(self.db_store, "load_profile_settings"):
            return settings
        try:
            runtime_settings = self.db_store.load_profile_settings()
        except Exception as exc:
            logger.warning("Failed to load exit profile settings: %s", exc)
            return settings
        for profile_id, profile in runtime_settings.items():
            resolved = settings.setdefault(profile_id, {"target_hold_days": 3.0})
            for key in ("target_hold_days", "basis_target_spread", "basis_adverse_stop_spread"):
                if profile.get(key) is not None:
                    resolved[key] = float(profile[key])
        return settings

    def _record_risk(self, event_type: str, severity: str, details: Dict[str, Any]) -> None:
        if self.db_store is not None and hasattr(self.db_store, "record_risk_event"):
            self.db_store.record_risk_event(event_type, severity, details)
        if hasattr(self.notifier, "notify_risk_alert"):
            try:
                self.notifier.notify_risk_alert(event_type, severity, details)
            except Exception as exc:
                logger.warning("Telegram notify_risk_alert failed: %s", exc)

    def activate_kill_switch(self, reason: str) -> None:
        self.redis.set("core:kill_switch", "true")
        self.notifier.send(f"[CRITICAL] Kill-switch activated: {reason}")
        self._record_risk("KILL_SWITCH", "CRITICAL", {"reason": reason})

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
        self,
        expected_positions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Compare expected positions (from Redis `core:positions` or argument) with
        actual exchange positions and detect 1:1 quantity drift between legs.
        """
        positions = self._load_positions(expected_positions)
        drifts: List[Dict[str, Any]] = []
        unavailable: List[Dict[str, Any]] = []
        for pos in positions:
            drift, data_issue = self._reconcile_position(pos)
            if drift:
                drifts.append(drift)
            if data_issue:
                unavailable.append(data_issue)

        if drifts:
            status = "DRIFT_DETECTED"
        elif unavailable:
            status = "DATA_UNAVAILABLE"
        else:
            status = "OK"
        return {"status": status, "drifts": drifts, "unavailable": unavailable}

    def _reconcile_position(
        self, pos: Dict[str, Any]
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
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
            return None, data_issues
        actual_a = float(actual_a)
        actual_b = float(actual_b)
        leg_diff = abs(actual_a - actual_b)
        drift = max(leg_diff, abs(actual_a - expected_qty), abs(actual_b - expected_qty))
        if drift <= self.drift_tolerance:
            self._position_alerts.pop(base_asset, None)
            return None, None
        drift_info = self._position_drift_details(pos, expected_qty, actual_a, actual_b, leg_diff)
        if self._should_report_position_alert(base_asset, "POSITION_DRIFT", drift_info):
            self._report_position_drift(drift_info)
        return drift_info, None

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
        if previous and previous[0] == fingerprint:
            if now_ms - previous[1] < POSITION_ALERT_INTERVAL_MS:
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

    def _okx_margin_ratio(self, info: Dict[str, Any]) -> Optional[float]:
        data = info.get("data")
        if not isinstance(data, list) or not data:
            return None
        imr = float(data[0].get("imr") or 0.0)
        eq = float(data[0].get("totalEq") or 0.0)
        return (imr / eq) if (eq > 0 and imr >= 0) else None

    def _bybit_margin_ratio(self, info: Dict[str, Any]) -> Optional[float]:
        res_list = (info.get("result") or {}).get("list")
        if not isinstance(res_list, list) or not res_list:
            return None
        rate = res_list[0].get("accountIMRate") or res_list[0].get("accountMMRate")
        return float(rate) if rate not in (None, "") else None

    def _ratio_from_balance_payload(self, bal: Dict[str, Any]) -> Optional[float]:
        info = bal.get("info") or {}
        okx_ratio = self._okx_margin_ratio(info)
        if okx_ratio is not None:
            return okx_ratio
        bybit_ratio = self._bybit_margin_ratio(info)
        if bybit_ratio is not None:
            return bybit_ratio
        used_usdt = float((bal.get("used") or {}).get("USDT") or 0.0)
        total_usdt = float((bal.get("total") or {}).get("USDT") or 0.0)
        return (used_usdt / total_usdt) if total_usdt > 0 else None

    def _extract_exchange_margin_ratio(self, client: Any) -> Optional[float]:
        if hasattr(client, "fetch_balance"):
            try:
                ratio = self._ratio_from_balance_payload(client.fetch_balance())
                if ratio is not None:
                    return ratio
            except Exception as exc:
                logger.warning("Failed to extract margin ratio: %s", exc)
        if hasattr(client, "fetch_margin_ratio"):
            return float(client.fetch_margin_ratio())
        return None

    def check_margin_ratios(self, margin_snapshots: Optional[Dict[str, float]] = None) -> Dict[str, str]:
        """
        Evaluate margin usage per exchange:
        - > 60%: send warning Telegram alert
        - > 75%: set core:kill_switch="true" and send critical Telegram alert
        """
        if margin_snapshots is None:
            margin_snapshots = {}
            for ex_name, client in self.exchanges.items():
                ratio = self._extract_exchange_margin_ratio(client)
                if ratio is not None:
                    margin_snapshots[ex_name] = ratio

        statuses: Dict[str, str] = {}
        for ex_name, ratio in margin_snapshots.items():
            usage = float(ratio)
            if usage > self.margin_critical_threshold:
                statuses[ex_name] = "CRITICAL"
                self.activate_kill_switch(f"{ex_name} margin_usage={usage:.2%} > {self.margin_critical_threshold:.0%}")
            elif usage > self.margin_warning_threshold:
                statuses[ex_name] = "WARNING"
                self.notifier.send(
                    f"[WARNING] {ex_name} margin usage high: {usage:.2%} (threshold {self.margin_warning_threshold:.0%})"
                )
            else:
                statuses[ex_name] = "OK"
        return statuses

    def _execute_unwind_and_finalize(self, position: Dict[str, Any], trigger: str) -> Dict[str, Any]:
        base_asset = position.get("base_asset", "UNKNOWN")
        if self.unwind_callback is None:
            return {"status": "UNWIND_RECOMMENDED"}
        try:
            result = self.unwind_callback(position)
        except Exception as exc:
            result = {"status": "UNWIND_PENDING", "error": str(exc)}
        if self.finalize_verified_close(position, result, trigger):
            return result
        details = {"base_asset": base_asset, "trigger": trigger, "unwind_result": result}
        self._record_risk("UNWIND_UNVERIFIED", "CRITICAL", details)
        self.activate_kill_switch(f"{trigger} unwind not verified for {base_asset}")
        return result

    def finalize_verified_close(
        self, position: Dict[str, Any], result: Dict[str, Any], close_reason: str
    ) -> bool:
        verified = result.get("status") == "UNWOUND_VERIFIED" and result.get("verified_flat") is True
        if not verified:
            return False
        pnl = calculate_realized_price_pnl(position, result)
        result["realized_pnl_usdt"] = pnl
        result["realized_pnl_scope"] = "gross_price_before_fees_and_funding"
        result["close_reason"] = close_reason
        base_asset = position.get("base_asset", "UNKNOWN")
        self.mark_position_closed(base_asset, pnl, close_reason)
        if hasattr(self.notifier, "notify_position_closed"):
            try:
                self.notifier.notify_position_closed(result, base_asset)
            except Exception as exc:
                logger.warning("Telegram notify_position_closed failed: %s", exc)
        return True

    def check_adl_indicators(self, positions: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """
        Check ADL indicator light (1..5). If adl_rank >= adl_threshold (4),
        alert Telegram and trigger early unwind when unwind_callback is set.
        """
        active_positions = self._load_positions(positions)
        unwound: List[Dict[str, Any]] = []
        for pos in active_positions:
            adl_a = self._extract_leg_adl_rank(pos.get("leg_a", {}))
            adl_b = self._extract_leg_adl_rank(pos.get("leg_b", {}))
            max_adl = max(adl_a, adl_b, int(pos.get("adl_rank", 0)))
            if max_adl < self.adl_threshold:
                continue
            base_asset = pos.get("base_asset", "UNKNOWN")
            self.notifier.send(
                f"[ADL WARNING] {base_asset} ADL rank={max_adl}/5 >= {self.adl_threshold}; triggering early unwind"
            )
            self._record_risk("ADL_HIGH_RISK", "WARNING", {"base_asset": base_asset, "adl_rank": max_adl})
            unwind_res = self._execute_unwind_and_finalize(pos, "ADL")
            unwound.append({"base_asset": base_asset, "adl_rank": max_adl, "unwind": unwind_res})
        return unwound

    def _parse_position_adl(self, p: Dict[str, Any]) -> int:
        info = p.get("info") or {}
        raw_adl = p.get("adl") or info.get("adl") or info.get("adlRankIndicator") or info.get("adlLevel") or 0
        return int(float(raw_adl))

    def _extract_leg_adl_rank(self, leg: Dict[str, Any]) -> int:
        if leg.get("adl_rank") is not None:
            return int(leg["adl_rank"])
        ex_name = leg.get("exchange", "")
        symbol = str(leg.get("symbol") or leg.get("canon_symbol") or leg.get("raw_symbol") or "")
        client = self.exchanges.get(ex_name)
        if not client or not hasattr(client, "fetch_positions") or PERP_SUFFIX not in symbol:
            return 0
        try:
            for p in client.fetch_positions([symbol]):
                p_sym = p.get("symbol") or (p.get("info") or {}).get("symbol")
                if p_sym == symbol:
                    return self._parse_position_adl(p)
        except Exception as exc:
            logger.warning("Failed to fetch ADL indicator for %s on %s: %s", symbol, ex_name, exc)
        return 0

    def _should_exit_position(
        self, pos: Dict[str, Any], now_ms: int, settings: Dict[str, Any]
    ) -> tuple[Optional[str], List[str]]:
        return evaluate_position_exit(pos, now_ms, settings, self.exchanges)

    def _report_exit_data_issues(self, pos: Dict[str, Any], issues: List[str], now_ms: int) -> None:
        base_asset = str(pos.get("base_asset", "UNKNOWN"))
        if not issues:
            self._exit_data_alerted_at.pop(base_asset, None)
            return
        last_alert_ms = self._exit_data_alerted_at.get(base_asset, 0)
        if now_ms - last_alert_ms < EXIT_DATA_ALERT_INTERVAL_MS:
            return
        self._exit_data_alerted_at[base_asset] = now_ms
        details = {
            "base_asset": base_asset,
            "issue": "; ".join(issues),
            "action": "exit check skipped; position remains open until data recovers or max hold is reached",
        }
        self._record_risk("EXIT_DATA_UNAVAILABLE", "WARNING", details)
        if not hasattr(self.notifier, "notify_risk_alert"):
            self.notifier.send(f"[EXIT DATA WARNING] {base_asset}: {details['issue']}; max-hold exit remains active")

    def check_exit_conditions(
        self,
        positions: Optional[List[Dict[str, Any]]] = None,
        now_ms: Optional[int] = None,
        max_hold_days: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Auto-close positions on funding reversal, basis limits, or profile hold duration."""
        curr_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        exited: List[Dict[str, Any]] = []
        profile_settings = self._load_exit_profile_settings()
        for pos in self._load_positions(positions):
            settings = dict(profile_settings.get(pos.get("profile_id"), {"target_hold_days": 3.0}))
            if max_hold_days is not None:
                settings["target_hold_days"] = max_hold_days
            reason, issues = self._should_exit_position(pos, curr_ms, settings)
            self._report_exit_data_issues(pos, issues, curr_ms)
            if not reason:
                continue
            base_asset = pos.get("base_asset", "UNKNOWN")
            self.notifier.send(f"[EXIT] Closing {base_asset}: {reason}")
            self._record_risk("AUTO_EXIT", "INFO", {"base_asset": base_asset, "reason": reason})
            unwind_res = self._execute_unwind_and_finalize(pos, f"AUTO_EXIT:{reason}")
            exited.append({"base_asset": base_asset, "reason": reason, "unwind": unwind_res})
        return exited

    def mark_position_closed(
        self,
        base_asset: str,
        realized_pnl_usdt: Optional[float] = None,
        close_reason: Optional[str] = None,
    ) -> None:
        if self.db_store is not None and hasattr(self.db_store, "close_active_position"):
            self.db_store.close_active_position(base_asset, realized_pnl_usdt, close_reason)
        raw = self.redis.get(REDIS_POSITIONS_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw:
            try:
                positions = [p for p in json.loads(raw) if p.get("base_asset") != base_asset]
                self.redis.set(REDIS_POSITIONS_KEY, json.dumps(positions))
            except Exception:
                pass
