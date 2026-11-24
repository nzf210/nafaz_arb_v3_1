"""Risk watchdog: position/balance reconciliation, margin ratio & ADL monitoring, kill-switch, Telegram alerts."""

import json
import logging
import os
import urllib.request
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MARGIN_WARNING_THRESHOLD = 0.60
MARGIN_CRITICAL_THRESHOLD = 0.75
ADL_HIGH_RISK_THRESHOLD = 4
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
        if self.dry_run or not self.bot_token or not self.chat_id:
            return True
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({"chat_id": self.chat_id, "text": message}).encode("utf-8")
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

    def _record_risk(self, event_type: str, severity: str, details: Dict[str, Any]) -> None:
        if self.db_store is not None and hasattr(self.db_store, "record_risk_event"):
            self.db_store.record_risk_event(event_type, severity, details)

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
        for pos in positions:
            base_asset = pos.get("base_asset", "UNKNOWN")
            expected_qty = float(pos.get("qty", 0.0))
            actual_a = self._fetch_leg_qty(pos["leg_a"])
            actual_b = self._fetch_leg_qty(pos["leg_b"])
            leg_diff = abs(actual_a - actual_b)
            if max(leg_diff, abs(actual_a - expected_qty), abs(actual_b - expected_qty)) > self.drift_tolerance:
                drift_info = {
                    "base_asset": base_asset,
                    "expected_qty": expected_qty,
                    "actual_qty_a": actual_a,
                    "actual_qty_b": actual_b,
                    "leg_drift": leg_diff,
                }
                drifts.append(drift_info)
                self._record_risk("POSITION_DRIFT", "WARNING", drift_info)
                self.notifier.send(
                    f"[DRIFT] {base_asset} drift detected: expected={expected_qty}, "
                    f"leg_a={actual_a}, leg_b={actual_b}"
                )

        return {"status": "DRIFT_DETECTED" if drifts else "OK", "drifts": drifts}

    def _find_position_qty(self, client: Any, symbol: str) -> Optional[float]:
        for p in client.fetch_positions([symbol]):
            p_sym = p.get("symbol") or (p.get("info") or {}).get("symbol")
            if p_sym == symbol:
                qty = p.get("contracts") if p.get("contracts") is not None else p.get("amount", 0.0)
                raw_ct = p.get("contractSize")
                if raw_ct is None and hasattr(client, "markets") and client.markets and symbol in client.markets:
                    raw_ct = client.markets[symbol].get("contractSize")
                ct_size = float(raw_ct or 1.0)
                return abs(float(qty or 0.0) * ct_size)
        return None

    def _fetch_leg_qty(self, leg: Dict[str, Any]) -> float:
        if "actual_qty" in leg:
            return abs(float(leg["actual_qty"]))
        client = self.exchanges.get(leg["exchange"])
        if not client:
            return 0.0
        symbol = str(leg.get("symbol") or leg.get("canon_symbol") or leg.get("raw_symbol") or "")
        is_perp = PERP_SUFFIX in symbol
        can_use_positions = hasattr(client, "fetch_positions") and (is_perp or not hasattr(client, "fetch_balance"))
        if can_use_positions:
            found = self._find_position_qty(client, symbol)
            if found is not None:
                return found
        if hasattr(client, "fetch_balance"):
            bal = client.fetch_balance()
            base = symbol.split("/")[0].split("-")[0].split("_")[0]
            return abs(float(bal.get("total", {}).get(base, 0.0) or 0.0))
        return 0.0

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
            unwind_res = self.unwind_callback(pos) if self.unwind_callback else {"status": "EARLY_UNWIND_RECOMMENDED"}
            if self.unwind_callback:
                self.mark_position_closed(base_asset)
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

    def _fetch_leg_funding_24h(self, leg: Dict[str, Any]) -> Optional[float]:
        symbol = str(leg.get("symbol") or leg.get("canon_symbol") or leg.get("raw_symbol") or "")
        if PERP_SUFFIX not in symbol:
            return 0.0
        interval_hours = max(int(leg.get("interval_hours") or 8), 1)
        if leg.get("current_funding_rate") is not None:
            return float(leg["current_funding_rate"]) * (24.0 / interval_hours)
        ex_name = leg.get("exchange", "")
        client = self.exchanges.get(ex_name)
        if not client or not hasattr(client, "fetch_funding_rate"):
            return None
        try:
            res = client.fetch_funding_rate(symbol)
            if not isinstance(res, dict) or res.get("fundingRate") is None:
                return None
            live_interval = int(res.get("intervalHours") or interval_hours)
            return float(res["fundingRate"]) * (24.0 / max(live_interval, 1))
        except Exception as exc:
            logger.warning("Failed to fetch funding rate for %s on %s: %s", symbol, ex_name, exc)
            return None

    def _should_exit_position(self, pos: Dict[str, Any], now_ms: int, max_hold_days: float) -> Optional[str]:
        opened_ms = int(pos.get("opened_at_ms") or 0)
        if opened_ms > 0 and max_hold_days > 0 and (now_ms - opened_ms) >= int(max_hold_days * 86400 * 1000):
            return "target_hold_days_reached"
        leg_a, leg_b = pos.get("leg_a", {}), pos.get("leg_b", {})
        short_24h = self._fetch_leg_funding_24h(leg_b)
        long_24h = self._fetch_leg_funding_24h(leg_a)
        if short_24h is None or long_24h is None:
            return None
        net_24h = short_24h - long_24h
        if net_24h < 0:
            return f"funding_flipped(net_24h={net_24h:.6f})"
        return None

    def check_exit_conditions(
        self,
        positions: Optional[List[Dict[str, Any]]] = None,
        now_ms: Optional[int] = None,
        max_hold_days: float = 3.0,
    ) -> List[Dict[str, Any]]:
        """Auto-close active positions when funding spread flips negative or hold duration expires."""
        import time

        curr_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        exited: List[Dict[str, Any]] = []
        for pos in self._load_positions(positions):
            reason = self._should_exit_position(pos, curr_ms, max_hold_days)
            if not reason:
                continue
            base_asset = pos.get("base_asset", "UNKNOWN")
            self.notifier.send(f"[EXIT] Closing {base_asset}: {reason}")
            self._record_risk("AUTO_EXIT", "INFO", {"base_asset": base_asset, "reason": reason})
            unwind_res = self.unwind_callback(pos) if self.unwind_callback else {"status": "EXIT_RECOMMENDED"}
            if self.unwind_callback:
                self.mark_position_closed(base_asset)
            exited.append({"base_asset": base_asset, "reason": reason, "unwind": unwind_res})
        return exited

    def mark_position_closed(self, base_asset: str) -> None:
        if self.db_store is not None and hasattr(self.db_store, "close_active_position"):
            self.db_store.close_active_position(base_asset)
        raw = self.redis.get(REDIS_POSITIONS_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw:
            try:
                positions = [p for p in json.loads(raw) if p.get("base_asset") != base_asset]
                self.redis.set(REDIS_POSITIONS_KEY, json.dumps(positions))
            except Exception:
                pass
