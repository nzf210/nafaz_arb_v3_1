"""PostgreSQL persistence store for runtime settings, signals, executions, positions, and risk events."""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PostgresStore:
    """Thin PostgreSQL persistence wrapper using psycopg v3 (with injectable connection for tests)."""

    def __init__(self, dsn: Optional[str] = None, conn: Optional[Any] = None):
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        self._conn = conn

    def _get_conn(self) -> Any:
        if self._conn is not None and not getattr(self._conn, "closed", False):
            return self._conn
        if not self.dsn:
            return None
        import psycopg

        self._conn = psycopg.connect(self.dsn, autocommit=True)
        return self._conn

    def sync_env_overrides(
        self,
        dry_run: Optional[bool] = None,
        canary_max_notional_usdt: Optional[float] = None,
    ) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            if dry_run is not None:
                cur.execute("UPDATE runtime_settings SET dry_run = %s, updated_at = NOW()", (bool(dry_run),))
            if canary_max_notional_usdt is not None:
                cur.execute(
                    "UPDATE runtime_settings SET canary_max_notional_usdt = %s, updated_at = NOW()",
                    (float(canary_max_notional_usdt),),
                )

    def ensure_schema(self, schema_sql_path: str = "configs/init.sql") -> None:
        conn = self._get_conn()
        if conn is None or not os.path.exists(schema_sql_path):
            return
        with open(schema_sql_path, "r", encoding="utf-8") as f:
            sql_text = f.read()
        with conn.cursor() as cur:
            cur.execute(sql_text)

    def load_profile_settings(self) -> Dict[str, Dict[str, Any]]:
        conn = self._get_conn()
        if conn is None:
            return {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT profile_id, enabled, dry_run, canary_max_notional_usdt, max_notional_usdt "
                "FROM runtime_settings"
            )
            rows = cur.fetchall()
        profiles: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            pid, enabled, dry_run, canary_cap, max_notional = row
            profiles[str(pid)] = {
                "enabled": bool(enabled),
                "dry_run": bool(dry_run),
                "canary_max_notional_usdt": float(canary_cap) if canary_cap is not None else None,
                "max_notional_usdt": float(max_notional) if max_notional is not None else None,
            }
        return profiles

    def record_signal_outcome(self, signal: Dict[str, Any], result: Dict[str, Any]) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signal_audit (
                    signal_id, profile_id, route_type, base_asset,
                    target_notional_usdt, status, reason, payload, result
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    reason = EXCLUDED.reason,
                    result = EXCLUDED.result
                """,
                (
                    str(signal.get("signal_id", "unknown")),
                    str(signal.get("profile_id", "unknown")),
                    str(signal.get("route_type", "unknown")),
                    str(signal.get("base_asset", "unknown")),
                    float(signal.get("target_notional_usdt", 0.0)),
                    str(result.get("status", "UNKNOWN")),
                    result.get("reason"),
                    json.dumps(signal),
                    json.dumps(result),
                ),
            )

    def record_execution(self, signal: Dict[str, Any], result: Dict[str, Any]) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        status = str(result.get("status", "UNKNOWN"))
        qty = float(
            result.get("qty")
            or result.get("qty_filled_a")
            or result.get("qty_filled_b")
            or 0.0
        )
        sig_legs = signal.get("legs", {})
        leg_a = {
            **(result.get("leg_a") or sig_legs.get("leg_a", {})),
            "interval_hours": sig_legs.get("leg_a", {}).get("interval_hours", 8),
        }
        leg_b = {
            **(result.get("leg_b") or sig_legs.get("leg_b", {})),
            "interval_hours": sig_legs.get("leg_b", {}).get("interval_hours", 8),
        }
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trade_executions (
                    signal_id, profile_id, base_asset, status, dry_run,
                    qty, effective_notional_usdt, leg_a, leg_b
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(signal.get("signal_id", "unknown")),
                    str(signal.get("profile_id", "unknown")),
                    str(signal.get("base_asset", "unknown")),
                    status,
                    bool(result.get("dry_run", True)),
                    qty,
                    float(result.get("effective_notional_usdt", 0.0)),
                    json.dumps(leg_a),
                    json.dumps(leg_b),
                ),
            )
            if status in (
                "FILLED",
                "FILLED_1TO1",
                "PARTIAL_ADJUSTED",
                "PARTIAL_FILLED_1TO1",
                "DRY_RUN",
                "DRY_RUN_Simulated",
            ) and qty > 0:
                cur.execute(
                    """
                    INSERT INTO active_positions (base_asset, profile_id, qty, leg_a, leg_b, status, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'OPEN', NOW())
                    ON CONFLICT (base_asset) DO UPDATE SET
                        profile_id = EXCLUDED.profile_id,
                        qty = EXCLUDED.qty,
                        leg_a = EXCLUDED.leg_a,
                        leg_b = EXCLUDED.leg_b,
                        status = 'OPEN',
                        updated_at = NOW()
                    """,
                    (
                        str(signal["base_asset"]),
                        str(signal["profile_id"]),
                        qty,
                        json.dumps(leg_a),
                        json.dumps(leg_b),
                    ),
                )

    def has_open_position(self, base_asset: str) -> bool:
        for pos in self.load_active_positions():
            if pos.get("base_asset") == base_asset and float(pos.get("qty", 0.0)) > 0:
                return True
        return False

    def close_active_position(self, base_asset: str) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE active_positions SET status = 'CLOSED', updated_at = NOW() WHERE base_asset = %s",
                (str(base_asset),),
            )

    def load_active_positions(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT base_asset, profile_id, qty, leg_a, leg_b FROM active_positions WHERE status = 'OPEN'"
            )
            rows = cur.fetchall()
        positions: List[Dict[str, Any]] = []
        for base_asset, profile_id, qty, leg_a, leg_b in rows:
            positions.append(
                {
                    "base_asset": base_asset,
                    "profile_id": profile_id,
                    "qty": float(qty),
                    "leg_a": json.loads(leg_a) if isinstance(leg_a, str) else leg_a,
                    "leg_b": json.loads(leg_b) if isinstance(leg_b, str) else leg_b,
                }
            )
        return positions

    def record_risk_event(self, event_type: str, severity: str, details: Dict[str, Any]) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO risk_events (event_type, severity, details) VALUES (%s, %s, %s)",
                (event_type, severity, json.dumps(details)),
            )
