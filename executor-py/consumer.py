"""Redis signal consumer v3.1: schema validation, TTL check, kill-switch, asset lock, and profile routing."""

import json
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "3.1"
REDIS_POSITIONS_KEY = "core:positions"
REQUIRED_SIGNAL_FIELDS = (
    "schema_version",
    "signal_id",
    "profile_id",
    "route_type",
    "base_asset",
    "target_notional_usdt",
    "legs",
    "expires_at_ms",
)
REQUIRED_LEG_FIELDS = ("exchange", "raw_symbol", "action", "vwap_price")


def validate_signal_schema(signal: Dict[str, Any]) -> None:
    if not isinstance(signal, dict):
        raise ValueError("Signal payload must be a dict")
    missing = [f for f in REQUIRED_SIGNAL_FIELDS if f not in signal or signal[f] is None]
    if missing:
        raise ValueError(f"Missing required signal fields: {', '.join(missing)}")
    if str(signal.get("schema_version")) != SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema_version: {signal.get('schema_version')}")
    if float(signal["target_notional_usdt"]) <= 0:
        raise ValueError("target_notional_usdt must be > 0")
    legs = signal.get("legs")
    if not isinstance(legs, dict) or "leg_a" not in legs or "leg_b" not in legs:
        raise ValueError("legs must contain leg_a and leg_b")
    for leg_key in ("leg_a", "leg_b"):
        leg = legs[leg_key]
        if not isinstance(leg, dict):
            raise ValueError(f"{leg_key} must be a dict")
        missing_leg = [f for f in REQUIRED_LEG_FIELDS if f not in leg or leg[f] is None]
        if missing_leg:
            raise ValueError(f"Missing {leg_key} fields: {', '.join(missing_leg)}")


def is_signal_expired(signal: Dict[str, Any], now_ms: Optional[int] = None) -> bool:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return current_ms >= int(signal["expires_at_ms"])


class SignalConsumer:
    """Consumes v3.1 arbitrage signals with pre-order safety gates and profile routing."""

    def __init__(
        self,
        redis_client: Any,
        executor: Any,
        profiles: Optional[Dict[str, Dict[str, Any]]] = None,
        default_dry_run: bool = False,
        canary_max_notional_usdt: Optional[float] = None,
        lock_ttl_sec: int = 10,
        clock_ms: Optional[Callable[[], int]] = None,
        db_store: Optional[Any] = None,
        notifier: Optional[Any] = None,
    ):
        self.redis = redis_client
        self.executor = executor
        self.db_store = db_store
        self.notifier = notifier
        # ponytail: in-memory profile dict; refreshes from Postgres runtime_settings when db_store is injected
        self.profiles = profiles or {
            "funding_carry_core": {"enabled": True},
            "basis_convergence_fast": {"enabled": True},
        }
        self.default_dry_run = default_dry_run
        self.canary_max_notional_usdt = canary_max_notional_usdt
        self.lock_ttl_sec = lock_ttl_sec
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def refresh_profiles_from_db(self) -> None:
        if self.db_store is not None and hasattr(self.db_store, "load_profile_settings"):
            db_profiles = self.db_store.load_profile_settings()
            if db_profiles:
                self.profiles.update(db_profiles)

    def is_kill_switch_active(self) -> bool:
        val = self.redis.get("core:kill_switch")
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        return str(val).strip().lower() in ("true", "1", "yes", "on")

    def acquire_asset_lock(self, base_asset: str, signal_id: str) -> bool:
        key = f"core:lock:{base_asset}"
        return bool(self.redis.set(key, signal_id, nx=True, ex=self.lock_ttl_sec))

    def release_asset_lock(self, base_asset: str, signal_id: Optional[str] = None) -> None:
        key = f"core:lock:{base_asset}"
        if signal_id is not None:
            cur = self.redis.get(key)
            if isinstance(cur, bytes):
                cur = cur.decode("utf-8")
            if cur is not None and str(cur) != str(signal_id):
                return
        self.redis.delete(key)

    def resolve_execution_params(self, signal: Dict[str, Any]) -> tuple[bool, float]:
        profile_id = signal["profile_id"]
        profile_cfg = self.profiles.get(profile_id, {"enabled": True})
        if not profile_cfg.get("enabled", True):
            raise ValueError(f"Profile {profile_id} is disabled")

        dry_run = bool(profile_cfg.get("dry_run", self.default_dry_run))
        notional = float(signal["target_notional_usdt"])

        canary_cap = profile_cfg.get("canary_max_notional_usdt", self.canary_max_notional_usdt)
        if canary_cap is not None and float(canary_cap) > 0:
            notional = min(notional, float(canary_cap))
        max_notional = profile_cfg.get("max_notional_usdt")
        if max_notional is not None and float(max_notional) > 0:
            notional = min(notional, float(max_notional))
        return dry_run, notional

    def process_signal(self, raw_signal: Any) -> Dict[str, Any]:
        if isinstance(raw_signal, (str, bytes)):
            signal = json.loads(raw_signal)
        else:
            signal = raw_signal

        try:
            validate_signal_schema(signal)
        except ValueError as exc:
            logger.warning("Invalid signal schema: %s", exc)
            return {"status": "REJECTED", "reason": "invalid_schema", "detail": str(exc)}

        signal_id = signal["signal_id"]
        now_ms = self.clock_ms()
        if is_signal_expired(signal, now_ms=now_ms):
            logger.warning("Signal expired. Skipped: %s", signal_id)
            return {"status": "REJECTED", "reason": "signal_expired", "signal_id": signal_id}

        if self.is_kill_switch_active():
            logger.warning("Global kill-switch active. Skipped: %s", signal_id)
            return {"status": "REJECTED", "reason": "kill_switch_active", "signal_id": signal_id}

        self.refresh_profiles_from_db()
        try:
            dry_run, effective_notional = self.resolve_execution_params(signal)
        except ValueError as exc:
            res = {"status": "REJECTED", "reason": "profile_disabled", "detail": str(exc), "signal_id": signal_id}
            self._persist_outcome(signal, res, executed=False)
            return res

        base_asset = signal["base_asset"]
        if self._has_open_position(base_asset):
            logger.info("Position already OPEN for %s. Skipped: %s", base_asset, signal_id)
            res = {
                "status": "REJECTED",
                "reason": "position_already_open",
                "base_asset": base_asset,
                "signal_id": signal_id,
            }
            self._persist_outcome(signal, res, executed=False)
            return res

        if not self.acquire_asset_lock(base_asset, signal_id):
            logger.info("Asset lock busy for %s. Skipped: %s", base_asset, signal_id)
            res = {"status": "REJECTED", "reason": "asset_locked", "base_asset": base_asset, "signal_id": signal_id}
            self._persist_outcome(signal, res, executed=False)
            return res

        try:
            result = self.executor.execute_signal(
                signal,
                dry_run=dry_run,
                effective_notional_usdt=effective_notional,
            )
            result["effective_notional_usdt"] = effective_notional
            result["dry_run"] = dry_run
            self._persist_outcome(signal, result, executed=True)
            return result
        finally:
            self.release_asset_lock(base_asset, signal_id)

    def _has_open_position(self, base_asset: str) -> bool:
        if self.db_store is not None and hasattr(self.db_store, "has_open_position"):
            if self.db_store.has_open_position(base_asset):
                return True
        raw = self.redis.get(REDIS_POSITIONS_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw:
            try:
                positions = json.loads(raw)
                for p in positions:
                    if p.get("base_asset") == base_asset and float(p.get("qty", 0.0)) > 0:
                        return True
            except Exception:
                pass
        return False

    def _persist_outcome(self, signal: Dict[str, Any], result: Dict[str, Any], executed: bool) -> None:
        status = str(result.get("status", ""))
        qty = float(result.get("qty") or result.get("qty_filled_a") or 0.0)
        if executed and status in (
            "FILLED",
            "FILLED_1TO1",
            "PARTIAL_ADJUSTED",
            "PARTIAL_FILLED_1TO1",
            "DRY_RUN",
            "DRY_RUN_Simulated",
        ) and qty > 0:
            self._upsert_redis_position(signal, result, qty)
            if self.notifier and hasattr(self.notifier, "notify_position_opened"):
                try:
                    self.notifier.notify_position_opened(signal, result)
                except Exception as exc:
                    logger.warning("Telegram notify_position_opened failed: %s", exc)
        if self.db_store is None:
            return
        if hasattr(self.db_store, "record_signal_outcome"):
            self.db_store.record_signal_outcome(signal, result)
        if executed and hasattr(self.db_store, "record_execution"):
            self.db_store.record_execution(signal, result)

    def _upsert_redis_position(self, signal: Dict[str, Any], result: Dict[str, Any], qty: float) -> None:
        raw = self.redis.get(REDIS_POSITIONS_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        positions = []
        if raw:
            try:
                positions = json.loads(raw)
            except Exception:
                positions = []
        base_asset = signal["base_asset"]
        leg_a = result.get("leg_a") or signal["legs"]["leg_a"]
        leg_b = result.get("leg_b") or signal["legs"]["leg_b"]
        entry = {
            "base_asset": base_asset,
            "profile_id": signal["profile_id"],
            "qty": qty,
            "opened_at_ms": self.clock_ms(),
            "leg_a": {
                **leg_a,
                "interval_hours": signal["legs"]["leg_a"].get("interval_hours", 8),
            },
            "leg_b": {
                **leg_b,
                "interval_hours": signal["legs"]["leg_b"].get("interval_hours", 8),
            },
        }
        positions = [p for p in positions if p.get("base_asset") != base_asset]
        positions.append(entry)
        self.redis.set(REDIS_POSITIONS_KEY, json.dumps(positions))
