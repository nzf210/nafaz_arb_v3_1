"""PostgreSQL persistence store for runtime settings, signals, executions, positions, and risk events."""

import json
import logging
import os
import time
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
        max_open_positions: Optional[int] = None,
        max_leg_drawdown_pct: Optional[float] = None,
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
            if max_open_positions is not None:
                cur.execute(
                    "UPDATE runtime_settings SET max_open_positions = %s, updated_at = NOW()",
                    (int(max_open_positions),),
                )
            if max_leg_drawdown_pct is not None:
                cur.execute(
                    "UPDATE runtime_settings SET max_leg_drawdown_pct = %s, updated_at = NOW()",
                    (float(max_leg_drawdown_pct),),
                )

    def update_runtime_setting(
        self, setting_name: str, value: Any, profile_id: Optional[str] = None
    ) -> bool:
        allowed = {
            "dry_run",
            "canary_max_notional_usdt",
            "max_notional_usdt",
            "max_open_positions",
            "max_leg_drawdown_pct",
            "min_profit_usdt",
            "max_loss_usdt",
            "allow_loss_on_hold",
        }
        if setting_name not in allowed:
            return False
        conn = self._get_conn()
        if conn is None:
            return False
        with conn.cursor() as cur:
            if profile_id:
                cur.execute(
                    f"UPDATE runtime_settings SET {setting_name} = %s, updated_at = NOW() WHERE profile_id = %s",
                    (value, profile_id),
                )
            else:
                cur.execute(
                    f"UPDATE runtime_settings SET {setting_name} = %s, updated_at = NOW()",
                    (value,),
                )
        return True

    def get_active_positions_count(self) -> int:
        conn = self._get_conn()
        if conn is None:
            return 0
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM active_positions WHERE status = 'OPEN'")
            if hasattr(cur, "fetchone"):
                row = cur.fetchone()
            else:
                rows = cur.fetchall() if hasattr(cur, "fetchall") else []
                row = rows[0] if rows else None
            return int(row[0]) if row and len(row) > 0 and row[0] is not None else 0

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
                "SELECT profile_id, enabled, dry_run, canary_max_notional_usdt, max_notional_usdt, "
                "target_hold_days, basis_target_spread, basis_adverse_stop_spread, "
                "min_profit_usdt, max_loss_usdt, allow_loss_on_hold, "
                "funding_flip_confirm_samples, funding_flip_confirm_window_ms, "
                "max_leg_drawdown_pct, max_open_positions "
                "FROM runtime_settings"
            )
            rows = cur.fetchall()
        profiles: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            values = list(row)
            if len(values) < 15:
                values.extend([None] * (15 - len(values)))
            (
                pid, enabled, dry_run, canary_cap, max_notional, target_hold_days,
                basis_target_spread, basis_adverse_stop_spread,
                min_profit_usdt, max_loss_usdt, allow_loss_on_hold,
                funding_flip_confirm_samples, funding_flip_confirm_window_ms,
                max_leg_drawdown_pct, max_open_positions,
            ) = values
            profiles[str(pid)] = {
                "enabled": bool(enabled),
                "dry_run": bool(dry_run),
                "canary_max_notional_usdt": float(canary_cap) if canary_cap is not None else None,
                "max_notional_usdt": float(max_notional) if max_notional is not None else None,
                "target_hold_days": float(target_hold_days) if target_hold_days is not None else None,
                "basis_target_spread": float(basis_target_spread) if basis_target_spread is not None else None,
                "basis_adverse_stop_spread": (
                    float(basis_adverse_stop_spread) if basis_adverse_stop_spread is not None else None
                ),
                "min_profit_usdt": float(min_profit_usdt) if min_profit_usdt is not None else None,
                "max_loss_usdt": float(max_loss_usdt) if max_loss_usdt is not None else None,
                "allow_loss_on_hold": (
                    bool(allow_loss_on_hold) if allow_loss_on_hold is not None else None
                ),
                "funding_flip_confirm_samples": (
                    int(funding_flip_confirm_samples) if funding_flip_confirm_samples is not None else 3
                ),
                "funding_flip_confirm_window_ms": (
                    int(funding_flip_confirm_window_ms) if funding_flip_confirm_window_ms is not None else 300000
                ),
                "max_leg_drawdown_pct": (
                    float(max_leg_drawdown_pct) if max_leg_drawdown_pct is not None else 0.60
                ),
                "max_open_positions": (
                    int(max_open_positions) if max_open_positions is not None else 3
                ),
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
                    float(result.get("effective_notional_usdt") or signal.get("target_notional_usdt", 0.0)),
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
        opened_at_ms = int(
            result.get("opened_at_ms") or signal.get("generated_at_ms") or time.time() * 1000
        )
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
                    INSERT INTO active_positions (
                        base_asset, profile_id, qty, leg_a, leg_b, status, updated_at, opened_at_ms
                    ) VALUES (%s, %s, %s, %s, %s, 'OPEN', NOW(), %s)
                    ON CONFLICT (base_asset) DO UPDATE SET
                        profile_id = EXCLUDED.profile_id,
                        qty = EXCLUDED.qty,
                        leg_a = EXCLUDED.leg_a,
                        leg_b = EXCLUDED.leg_b,
                        status = 'OPEN',
                        updated_at = NOW(),
                        opened_at_ms = EXCLUDED.opened_at_ms,
                        realized_pnl_usdt = NULL,
                        close_reason = NULL,
                        closed_at = NULL
                    """,
                    (
                        str(signal["base_asset"]),
                        str(signal["profile_id"]),
                        qty,
                        json.dumps(leg_a),
                        json.dumps(leg_b),
                        opened_at_ms,
                    ),
                )

    def has_open_position(self, base_asset: str) -> bool:
        for pos in self.load_active_positions():
            if pos.get("base_asset") == base_asset and float(pos.get("qty", 0.0)) > 0:
                return True
        return False

    def close_active_position(
        self, base_asset: str, realized_pnl_usdt: Optional[float] = None, close_reason: Optional[str] = None
    ) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE active_positions
                SET status = 'CLOSED', realized_pnl_usdt = %s, close_reason = %s,
                    closed_at = NOW(), updated_at = NOW()
                WHERE base_asset = %s
                """,
                (realized_pnl_usdt, close_reason, str(base_asset)),
            )

    def load_active_positions(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT base_asset, profile_id, qty, leg_a, leg_b, opened_at_ms "
                "FROM active_positions WHERE status = 'OPEN'"
            )
            rows = cur.fetchall()
        positions: List[Dict[str, Any]] = []
        for base_asset, profile_id, qty, leg_a, leg_b, opened_at_ms in rows:
            positions.append(
                {
                    "base_asset": base_asset,
                    "profile_id": profile_id,
                    "qty": float(qty),
                    "leg_a": json.loads(leg_a) if isinstance(leg_a, str) else leg_a,
                    "leg_b": json.loads(leg_b) if isinstance(leg_b, str) else leg_b,
                    "opened_at_ms": int(opened_at_ms),
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

    def list_recent_executions(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, signal_id, profile_id, base_asset, status, dry_run,
                       qty, effective_notional_usdt, leg_a, leg_b, created_at
                FROM trade_executions
                ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "signal_id": r[1],
                    "profile_id": r[2],
                    "base_asset": r[3],
                    "status": r[4],
                    "dry_run": r[5],
                    "qty": float(r[6]),
                    "effective_notional_usdt": float(r[7]),
                    "leg_a": json.loads(r[8]) if isinstance(r[8], str) else r[8],
                    "leg_b": json.loads(r[9]) if isinstance(r[9], str) else r[9],
                    "created_at": str(r[10]),
                }
                for r in rows
            ]

    def list_recent_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT signal_id, profile_id, route_type, base_asset,
                       target_notional_usdt, status, reason, created_at, result
                FROM signal_audit
                ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                raw_res = r[8] if len(r) > 8 else None
                res_obj = json.loads(raw_res) if isinstance(raw_res, str) else (raw_res or {})
                eff = res_obj.get("effective_notional_usdt") if isinstance(res_obj, dict) else None
                ex = res_obj.get("exchange") if isinstance(res_obj, dict) else None
                out.append(
                    {
                        "signal_id": r[0],
                        "profile_id": r[1],
                        "route_type": r[2],
                        "base_asset": r[3],
                        "target_notional_usdt": float(eff if eff is not None else r[4]),
                        "status": r[5],
                        "reason": r[6],
                        "exchange": ex,
                        "created_at": str(r[7]),
                    }
                )
            return out

    def list_recent_risk_events(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, event_type, severity, details, created_at FROM risk_events ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "event_type": r[1],
                    "severity": r[2],
                    "details": json.loads(r[3]) if isinstance(r[3], str) else r[3],
                    "created_at": str(r[4]),
                }
                for r in rows
            ]

    def record_closed_trade(
        self,
        position: Dict[str, Any],
        unwind_result: Dict[str, Any],
        pnl: Dict[str, Any],
        close_reason: Optional[str] = None,
    ) -> None:
        """Persist a finished position with its realized PnL for trade history."""
        conn = self._get_conn()
        if conn is None:
            return
        legs = pnl.get("legs") or {}
        leg_a = legs.get("leg_a") or {}
        leg_b = legs.get("leg_b") or {}
        src_a = position.get("leg_a") or {}
        src_b = position.get("leg_b") or {}
        base_params = (
            str(position.get("base_asset", "unknown")),
            str(position.get("profile_id", "")) or None,
            float(position.get("qty", 0.0) or 0.0),
            src_a.get("exchange"),
            src_b.get("exchange"),
            leg_a.get("entry_price"),
            leg_b.get("entry_price"),
            leg_a.get("exit_price"),
            leg_b.get("exit_price"),
            float(leg_a.get("qty", 0.0) or 0.0),
            float(leg_b.get("qty", 0.0) or 0.0),
            pnl.get("realized_pnl_usdt"),
            pnl.get("fees_usdt"),
            pnl.get("net_pnl_usdt"),
            str(pnl.get("pnl_status") or "unavailable"),
            close_reason,
            int(position.get("opened_at_ms") or 0) or None,
        )
        extended_params = base_params + (
            pnl.get("funding_usdt"),
            pnl.get("entry_fees_usdt"),
            pnl.get("exit_fees_usdt"),
        )
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO closed_trades (
                        base_asset, profile_id, qty, leg_a_exchange, leg_b_exchange,
                        entry_price_a, entry_price_b, exit_price_a, exit_price_b,
                        closed_qty_a, closed_qty_b, realized_pnl_usdt, fees_usdt, net_pnl_usdt,
                        pnl_status, close_reason, opened_at_ms, funding_usdt, entry_fees_usdt, exit_fees_usdt
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    extended_params,
                )
            except Exception:
                cur.execute(
                    """
                    INSERT INTO closed_trades (
                        base_asset, profile_id, qty, leg_a_exchange, leg_b_exchange,
                        entry_price_a, entry_price_b, exit_price_a, exit_price_b,
                        closed_qty_a, closed_qty_b, realized_pnl_usdt, fees_usdt, net_pnl_usdt,
                        pnl_status, close_reason, opened_at_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    base_params,
                )


    def list_closed_trades(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, base_asset, profile_id, qty, leg_a_exchange, leg_b_exchange,
                       entry_price_a, entry_price_b, exit_price_a, exit_price_b,
                       realized_pnl_usdt, fees_usdt, net_pnl_usdt, pnl_status,
                       close_reason, closed_at
                FROM closed_trades
                ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "base_asset": r[1],
                "profile_id": r[2],
                "qty": float(r[3] or 0.0),
                "leg_a_exchange": r[4],
                "leg_b_exchange": r[5],
                "entry_price_a": r[6],
                "entry_price_b": r[7],
                "exit_price_a": r[8],
                "exit_price_b": r[9],
                "realized_pnl_usdt": float(r[10]) if r[10] is not None else None,
                "fees_usdt": float(r[11] or 0.0),
                "net_pnl_usdt": float(r[12]) if r[12] is not None else None,
                "pnl_status": r[13],
                "close_reason": r[14],
                "closed_at": str(r[15]),
            }
            for r in rows
        ]

    def realized_pnl_summary(self) -> Dict[str, Any]:
        conn = self._get_conn()
        if conn is None:
            return {}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*),
                       COUNT(COALESCE(net_pnl_usdt, realized_pnl_usdt)),
                       COALESCE(SUM(realized_pnl_usdt), 0),
                       COALESCE(SUM(fees_usdt), 0),
                       COALESCE(SUM(COALESCE(net_pnl_usdt, realized_pnl_usdt - COALESCE(fees_usdt, 0))), 0),
                       COALESCE(SUM(CASE WHEN COALESCE(net_pnl_usdt, realized_pnl_usdt - COALESCE(fees_usdt, 0)) > 0 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN COALESCE(net_pnl_usdt, realized_pnl_usdt - COALESCE(fees_usdt, 0)) < 0 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN closed_at >= NOW() - INTERVAL '24 hours'
                                         THEN COALESCE(net_pnl_usdt, realized_pnl_usdt - COALESCE(fees_usdt, 0)) ELSE 0 END), 0)
                FROM closed_trades
                """
            )
            row = cur.fetchone() or (0, 0, 0.0, 0.0, 0.0, 0, 0, 0.0)
        count, priced, gross, fees, net, wins, losses, pnl_24h = row
        return {
            "trades": int(count),
            "priced_trades": int(priced),
            "unpriced_trades": int(count) - int(priced),
            "realized_pnl_usdt": float(gross),
            "fees_usdt": float(fees),
            "net_pnl_usdt": float(net),
            "wins": int(wins),
            "losses": int(losses),
            "pnl_24h_usdt": float(pnl_24h),
        }

    def update_profile_setting(self, profile_id: str, field: str, value: Any) -> bool:
        allowed = {
            "enabled": bool,
            "dry_run": bool,
            "canary_max_notional_usdt": float,
            "max_notional_usdt": float,
        }
        if field not in allowed:
            return False
        conn = self._get_conn()
        if conn is None:
            return False
        val = allowed[field](value)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE runtime_settings SET {field} = %s, updated_at = NOW() WHERE profile_id = %s",
                (val, profile_id),
            )
            return cur.rowcount > 0

