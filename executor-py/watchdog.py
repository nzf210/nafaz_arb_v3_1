"""Risk watchdog: margin ratio & ADL monitoring, kill-switch policy, unwind retries, exit scheduling."""

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from exit_monitor import evaluate_position_exit, fetch_leg_marks
from exit_policy import decide_exit
from exit_rules import DEFAULT_PROFILE_EXIT_SETTINGS
from notifier import TelegramNotifier
from pnl import estimate_net_exit_pnl, estimate_price_pnl
from reconcile import REDIS_POSITIONS_KEY, ReconcileMixin


logger = logging.getLogger(__name__)

MARGIN_WARNING_THRESHOLD = 0.60
MARGIN_CRITICAL_THRESHOLD = 0.75
ADL_HIGH_RISK_THRESHOLD = 4
EXIT_DATA_ALERT_INTERVAL_MS = 300_000
UNWIND_RETRY_INTERVAL_MS = 60_000
MAX_UNWIND_ATTEMPTS = 3
PERP_SUFFIX = ":USDT"
KILL_SWITCH_KEY = "core:kill_switch"
KILL_SWITCH_REASON_KEY = "core:kill_switch_reason"
AUTO_REASON_PREFIX = "auto:"
LEGACY_KILL_REASON = "legacy_unknown"

__all__ = ["RiskWatchdog", "TelegramNotifier", "REDIS_POSITIONS_KEY"]


class RiskWatchdog(ReconcileMixin):
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
        dry_run: bool = False,
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
        self.dry_run = dry_run
        self._exit_data_alerted_at: Dict[str, int] = {}
        self._exit_blocked_at: Dict[str, int] = {}
        self._position_alerts: Dict[str, tuple[str, int]] = {}
        self._unwind_attempts: Dict[str, tuple[int, int]] = {}
        self._margin_status: Dict[str, str] = {}
        self._funding_flip_confirmations: Dict[str, tuple[int, int]] = {}

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
            for key in (
                "target_hold_days",
                "basis_target_spread",
                "basis_adverse_stop_spread",
                "min_profit_usdt",
                "max_loss_usdt",
                "funding_flip_confirm_samples",
                "funding_flip_confirm_window_ms",
            ):
                if profile.get(key) is not None:
                    resolved[key] = float(profile[key])
            if profile.get("allow_loss_on_hold") is not None:
                resolved["allow_loss_on_hold"] = bool(profile["allow_loss_on_hold"])
        return settings

    def _record_risk(self, event_type: str, severity: str, details: Dict[str, Any]) -> None:
        if self.db_store is not None and hasattr(self.db_store, "record_risk_event"):
            self.db_store.record_risk_event(event_type, severity, details)
        if hasattr(self.notifier, "notify_risk_alert"):
            try:
                self.notifier.notify_risk_alert(event_type, severity, details)
            except Exception as exc:
                logger.warning("Telegram notify_risk_alert failed: %s", exc)

    def _kill_switch_active(self) -> bool:
        val = self.redis.get(KILL_SWITCH_KEY)
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        return str(val).strip().lower() in ("true", "1", "yes", "on")

    def activate_kill_switch(self, reason: str, auto: bool = False) -> None:
        """Halt new entries only; exits and monitoring keep running."""
        stored_reason = f"{AUTO_REASON_PREFIX}{reason}" if auto else reason
        already = self._kill_switch_active()
        self.redis.set(KILL_SWITCH_KEY, "true")
        self.redis.set(KILL_SWITCH_REASON_KEY, stored_reason)
        if already:
            return
        self.notifier.send(f"[CRITICAL] Kill-switch activated: {reason}")
        self._record_risk("KILL_SWITCH", "CRITICAL", {"reason": reason})

    def _kill_switch_reason(self) -> str:
        raw = self.redis.get(KILL_SWITCH_REASON_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        reason = str(raw or "").strip()
        return reason or LEGACY_KILL_REASON

    def maybe_clear_auto_kill_switch(self, healthy: bool = True) -> bool:
        """Resume entries after automatic halts once every remaining position reconciles cleanly."""
        if not self._kill_switch_active() or not healthy:
            return False
        reason = self._kill_switch_reason()
        if not reason.startswith(AUTO_REASON_PREFIX) and reason != LEGACY_KILL_REASON:
            return False
        self.redis.set(KILL_SWITCH_KEY, "false")
        self.redis.set(KILL_SWITCH_REASON_KEY, "")
        self.notifier.send(f"[RECOVERY] Kill-switch auto-cleared ({reason}); positions reconciled, entries resumed")
        self._record_risk("KILL_SWITCH_AUTO_CLEARED", "INFO", {"reason": reason})
        return True

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
            previous = self._margin_status.get(ex_name)
            if usage > self.margin_critical_threshold:
                statuses[ex_name] = "CRITICAL"
                self.activate_kill_switch(
                    f"{ex_name} margin_usage={usage:.2%} > {self.margin_critical_threshold:.0%}", auto=True
                )
            elif usage > self.margin_warning_threshold:
                statuses[ex_name] = "WARNING"
                if previous != "WARNING":
                    self.notifier.send(
                        f"[WARNING] {ex_name} margin usage high: {usage:.2%} "
                        f"(threshold {self.margin_warning_threshold:.0%})"
                    )
            else:
                statuses[ex_name] = "OK"
            self._margin_status[ex_name] = statuses[ex_name]
        return statuses

    def _unwind_attempt_allowed(self, base_asset: str) -> bool:
        attempts, last_ms = self._unwind_attempts.get(base_asset, (0, 0))
        if attempts == 0:
            return True
        return int(time.time() * 1000) - last_ms >= UNWIND_RETRY_INTERVAL_MS

    def _register_unwind_attempt(self, base_asset: str) -> int:
        attempts = self._unwind_attempts.get(base_asset, (0, 0))[0] + 1
        self._unwind_attempts[base_asset] = (attempts, int(time.time() * 1000))
        return attempts

    def _execute_unwind_and_finalize(self, position: Dict[str, Any], trigger: str) -> Dict[str, Any]:
        base_asset = position.get("base_asset", "UNKNOWN")
        if self.unwind_callback is None:
            return {"status": "UNWIND_RECOMMENDED"}
        if not self._unwind_attempt_allowed(base_asset):
            return {"status": "UNWIND_THROTTLED", "base_asset": base_asset}
        attempts = self._register_unwind_attempt(base_asset)
        try:
            result = self.unwind_callback(position)
        except Exception as exc:
            result = {"status": "UNWIND_PENDING", "error": str(exc)}
        if self.finalize_verified_close(position, result, trigger):
            return result
        details = {"base_asset": base_asset, "trigger": trigger, "attempt": attempts, "unwind_result": result}
        self._record_risk("UNWIND_UNVERIFIED", "CRITICAL", details)
        if attempts >= MAX_UNWIND_ATTEMPTS:
            self.activate_kill_switch(f"{trigger} unwind not verified for {base_asset}", auto=True)
        else:
            self.notifier.send(
                f"[UNWIND RETRY] {base_asset} ({trigger}) attempt {attempts}/{MAX_UNWIND_ATTEMPTS} "
                f"not verified; retrying in {UNWIND_RETRY_INTERVAL_MS // 1000}s (exits keep running)"
            )
        return result

    def finalize_verified_close(
        self, position: Dict[str, Any], result: Dict[str, Any], close_reason: str
    ) -> bool:
        status = result.get("status")
        verified = result.get("verified_flat") is True and status in ("UNWOUND_VERIFIED", "STALE_FLAT")
        if not verified:
            return False
        self._finalize_position(position, result, close_reason)
        self._unwind_attempts.pop(position.get("base_asset", ""), None)
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

    def _estimate_exit_pnl(self, pos: Dict[str, Any], curr_ms: Optional[int] = None) -> Optional[float]:
        marks, issue = fetch_leg_marks(pos, self.exchanges)
        if issue or marks is None:
            return None
        est = estimate_net_exit_pnl(pos, marks, exchanges=self.exchanges, now_ms=curr_ms)
        if est.get("data_status") == "unavailable":
            return None
        return est.get("net_pnl_usdt")


    def _confirm_funding_flip(
        self, pos: Dict[str, Any], settings: Dict[str, Any], curr_ms: int
    ) -> bool:
        base_asset = str(pos.get("base_asset", "UNKNOWN"))
        try:
            required = max(1, int(float(settings.get("funding_flip_confirm_samples", 3))))
            window_ms = max(0, int(float(settings.get("funding_flip_confirm_window_ms", 300_000))))
        except (TypeError, ValueError, OverflowError):
            required, window_ms = 3, 300_000
        previous = self._funding_flip_confirmations.get(base_asset)
        if previous is None or curr_ms - previous[1] > window_ms:
            count = 1
        else:
            count = previous[0] + 1
        self._funding_flip_confirmations[base_asset] = (count, curr_ms)
        if count >= required:
            self._funding_flip_confirmations.pop(base_asset, None)
            return True
        self._record_risk(
            "FUNDING_FLIP_PENDING_CONFIRMATION",
            "INFO",
            {"base_asset": base_asset, "confirmed_samples": count, "required_samples": required},
        )
        return False

    def _reset_funding_confirmation(self, pos: Dict[str, Any]) -> None:
        self._funding_flip_confirmations.pop(str(pos.get("base_asset", "UNKNOWN")), None)

    def _report_exit_blocked(self, pos: Dict[str, Any], reason: str, note: str, now_ms: int) -> None:
        base_asset = str(pos.get("base_asset", "UNKNOWN"))
        last_ms = self._exit_blocked_at.get(base_asset, 0)
        if now_ms - last_ms < EXIT_DATA_ALERT_INTERVAL_MS:
            return
        self._exit_blocked_at[base_asset] = now_ms
        details = {"base_asset": base_asset, "trigger": reason, "note": note}
        self._record_risk("EXIT_BLOCKED", "INFO", details)
        self.notifier.send(f"[EXIT HELD] {base_asset}: {reason} → {note}; position kept open")

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
                self._reset_funding_confirmation(pos)
                continue
            if not reason.startswith("funding_flipped"):
                self._reset_funding_confirmation(pos)
            if reason.startswith("funding_flipped") and not self._confirm_funding_flip(pos, settings, curr_ms):
                self._report_exit_blocked(pos, reason, "funding flip awaiting confirmation", curr_ms)
                continue
            pnl_estimate = self._estimate_exit_pnl(pos, curr_ms)
            allowed, note = decide_exit(reason, pnl_estimate, settings)
            if not allowed:
                self._report_exit_blocked(pos, reason, note, curr_ms)
                continue
            self._exit_blocked_at.pop(str(pos.get("base_asset", "UNKNOWN")), None)
            base_asset = pos.get("base_asset", "UNKNOWN")
            self.notifier.send(f"[EXIT] Closing {base_asset}: {reason} ({note})")
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
        if not raw:
            return
        try:
            positions = [p for p in json.loads(raw) if p.get("base_asset") != base_asset]
            self.redis.set(REDIS_POSITIONS_KEY, json.dumps(positions))
        except Exception:
            pass
