"""Comprehensive unit tests for nafaz_arb v3.1 executor-py (consumer, execution, watchdog)."""

import json
import pytest

from consumer import SignalConsumer, is_signal_expired, validate_signal_schema
from execution import (
    DeltaNeutralExecutor,
    compatible_step_size,
    compute_1to1_qty,
    round_price,
    round_qty,
    validate_min_notional,
)
from watchdog import RiskWatchdog, TelegramNotifier


class MockRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value, nx: bool = False, ex: int | None = None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def delete(self, key: str):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


class MockExchange:
    def __init__(self, name: str, fill_ratio: float = 1.0, fail_create: bool = False):
        self.name = name
        self.fill_ratio = fill_ratio
        self.fail_create = fail_create
        self.orders = []
        self.cancelled = []
        self.positions = []
        self.margin_ratio = 0.25
        self.markets = {}

    def create_order(self, symbol: str, order_type: str, side: str, amount: float, price=None, params=None):
        if self.fail_create:
            raise RuntimeError(f"{self.name} create_order timeout/failure")
        # Market unwind orders always fill 100%
        ratio = 1.0 if order_type == "market" else self.fill_ratio
        filled = round(amount * ratio, 8)
        status = "closed" if filled >= amount else "open"
        order = {
            "id": f"{self.name}_ord_{len(self.orders) + 1}",
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "filled": filled,
            "price": price,
            "average": price,
            "status": status,
        }
        self.orders.append(order)
        if order_type == "market":
            self.positions = []
        return order

    def cancel_order(self, order_id: str, symbol: str | None = None):
        self.cancelled.append((order_id, symbol))
        return {"id": order_id, "status": "canceled"}

    def fetch_balance(self):
        return {"free": {"USDT": 1_000_000.0}, "total": {"USDT": 1_000_000.0}}

    def fetch_positions(self, symbols=None):
        return self.positions

    def fetch_margin_ratio(self):
        return self.margin_ratio


def make_valid_signal(now_ms: int = 1700000000000, **overrides):
    sig = {
        "schema_version": "3.1",
        "signal_id": "sig_okx_bybit_btc_1700000000123",
        "dedup_key": "perp_perp:BTC:okx:bybit",
        "profile_id": "funding_carry_core",
        "route_type": "perp_perp_cross_cex",
        "base_asset": "BTC",
        "target_notional_usdt": 1000.0,
        "legs": {
            "leg_a": {
                "exchange": "okx",
                "market_type": "linear_perp",
                "raw_symbol": "BTC-USDT-SWAP",
                "canon_symbol": "BTC/USDT:USDT",
                "action": "BUY_LONG",
                "vwap_price": 50000.0,
                "funding_rate": -0.0001,
                "interval_hours": 8,
            },
            "leg_b": {
                "exchange": "bybit",
                "market_type": "linear_perp",
                "raw_symbol": "BTCUSDT",
                "canon_symbol": "BTC/USDT:USDT",
                "action": "SELL_SHORT",
                "vwap_price": 50010.0,
                "funding_rate": 0.0005,
                "interval_hours": 8,
            },
        },
        "metrics": {
            "gross_funding_24h": 0.0018,
            "round_trip_cost": 0.0012,
            "net_daily_yield": 0.001628,
            "breakeven_days": 0.67,
        },
        "generated_at_ms": now_ms,
        "expires_at_ms": now_ms + 2500,
    }
    sig.update(overrides)
    return sig


def test_consumer_rejects_expired_and_bad_schema():
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})

    now_ms = 1700000005000
    consumer = SignalConsumer(redis, executor, clock_ms=lambda: now_ms)

    # Expired signal (expires_at_ms < now_ms)
    expired_sig = make_valid_signal(now_ms=1700000000000)
    res_exp = consumer.process_signal(expired_sig)
    assert res_exp["status"] == "REJECTED"
    assert res_exp["reason"] == "signal_expired"
    assert len(ex_okx.orders) == 0

    # Bad schema version
    bad_schema_sig = make_valid_signal(now_ms=now_ms, schema_version="3.0")
    res_schema = consumer.process_signal(bad_schema_sig)
    assert res_schema["status"] == "REJECTED"
    assert res_schema["reason"] == "invalid_schema"


def test_consumer_respects_kill_switch_and_asset_lock():
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    now_ms = 1700000000000
    consumer = SignalConsumer(redis, executor, clock_ms=lambda: now_ms)

    # Kill-switch active
    redis.set("core:kill_switch", "true")
    res_ks = consumer.process_signal(make_valid_signal(now_ms=now_ms))
    assert res_ks["status"] == "REJECTED"
    assert res_ks["reason"] == "kill_switch_active"
    redis.delete("core:kill_switch")

    # Asset lock already held
    redis.set("core:lock:BTC", "other_signal_id", nx=True, ex=10)
    res_lock = consumer.process_signal(make_valid_signal(now_ms=now_ms))
    assert res_lock["status"] == "REJECTED"
    assert res_lock["reason"] == "asset_locked"
    redis.delete("core:lock:BTC")

    # Normal execution acquires and releases lock
    res_ok = consumer.process_signal(json.dumps(make_valid_signal(now_ms=now_ms)))
    assert res_ok["status"] == "FILLED_1TO1"
    assert res_ok["leg_a"]["entry_avg_price"] == res_ok["leg_a"]["price"]
    assert redis.get("core:lock:BTC") is None


def test_guardrails_rounding_and_1to1_qty():
    assert round_price(65000.567, 0.1, "buy") == pytest.approx(65000.5)
    assert round_qty(0.123456, 0.001) == pytest.approx(0.123)
    assert compatible_step_size(0.001, 0.01) == pytest.approx(0.01)

    gr_a = {"tick_size": 0.1, "step_size": 0.001, "min_notional": 10.0}
    gr_b = {"tick_size": 0.5, "step_size": 0.01, "min_notional": 10.0}
    qty = compute_1to1_qty(1000.0, 50000.0, 50000.0, gr_a, gr_b)
    assert qty == pytest.approx(0.02)

    with pytest.raises(ValueError, match="min_notional"):
        validate_min_notional(0.0001, 50000.0, 10.0, "leg_a")


def test_dry_run_and_canary_notional_cap():
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    now_ms = 1700000000000

    consumer = SignalConsumer(
        redis,
        executor,
        default_dry_run=True,
        canary_max_notional_usdt=250.0,
        clock_ms=lambda: now_ms,
    )
    sig = make_valid_signal(now_ms=now_ms, target_notional_usdt=1000.2)
    res = consumer.process_signal(sig)
    assert res["status"] == "DRY_RUN_Simulated"
    assert res["dry_run"] is True
    assert res["effective_notional_usdt"] == pytest.approx(250.0)
    assert len(ex_okx.orders) == 0
    assert len(ex_bybit.orders) == 0
    assert len(executor.dry_run_log) == 1


def test_partial_fill_leg_a_adjusts_leg_b_1to1():
    # Leg A fills 60%, Leg B fills 100% of adjusted amount
    ex_okx = MockExchange("okx", fill_ratio=0.6)
    ex_bybit = MockExchange("bybit", fill_ratio=1.0)
    guardrails = {
        "okx": {"tick_size": 0.1, "step_size": 0.001, "min_notional": 5.0},
        "bybit": {"tick_size": 0.1, "step_size": 0.001, "min_notional": 5.0},
    }
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit}, guardrails=guardrails)
    sig = make_valid_signal(target_notional_usdt=1000.2)
    res = executor.execute_signal(sig)

    assert res["status"] == "PARTIAL_FILLED_1TO1"
    assert res["cancelled_unfilled_a"] is True
    assert len(ex_okx.cancelled) == 1
    assert res["qty_filled_a"] == pytest.approx(0.012)
    assert res["qty_filled_b"] == pytest.approx(0.012)
    assert ex_bybit.orders[0]["amount"] == pytest.approx(0.012)


def test_leg_b_failure_triggers_emergency_unwind_on_leg_a():
    ex_okx = MockExchange("okx", fill_ratio=1.0)
    ex_bybit = MockExchange("bybit", fail_create=True)
    alerts = []
    executor = DeltaNeutralExecutor(
        {"okx": ex_okx, "bybit": ex_bybit},
        max_leg_b_retries=1,
        notifier=alerts.append,
    )
    sig = make_valid_signal(target_notional_usdt=1000.2)
    res = executor.execute_signal(sig)

    assert res["status"] == "EMERGENCY_UNWOUND"
    assert res["unwound_qty_a"] == pytest.approx(res["qty_filled_a"])
    # okx should have 2 orders: initial limit buy and emergency market sell
    assert len(ex_okx.orders) == 2
    assert ex_okx.orders[0]["side"] == "buy"
    assert ex_okx.orders[1]["type"] == "market"
    assert ex_okx.orders[1]["side"] == "sell"
    assert ex_okx.orders[1]["amount"] == pytest.approx(res["qty_filled_a"])
    assert any("Emergency unwind" in a for a in alerts)


def test_unwind_route_closes_both_legs():
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    ex_okx.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.05}]
    ex_bybit.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.05}]
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    position = {
        "base_asset": "BTC",
        "qty": 0.05,
        "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT", "side": "buy"},
        "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "side": "sell"},
    }
    res = executor.unwind_route(position)
    assert res["status"] == "UNWOUND_VERIFIED"
    assert res["verified_flat"] is True
    assert ex_okx.orders[0]["side"] == "sell"
    assert ex_bybit.orders[0]["side"] == "buy"


def test_watchdog_reconciliation_margin_and_adl():
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    ex_okx.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.02}]
    ex_bybit.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.015}]  # drift!

    notifier = TelegramNotifier(dry_run=True)
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit}, notifier=notifier.send)
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": ex_okx, "bybit": ex_bybit},
        notifier=notifier,
        unwind_callback=executor.unwind_route,
    )

    # 1. Position drift detection
    redis.set(
        "core:positions",
        json.dumps(
            [
                {
                    "base_asset": "BTC",
                    "qty": 0.02,
                    "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT"},
                    "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT"},
                }
            ]
        ),
    )
    rec = watchdog.reconcile_positions()
    assert rec["status"] == "DRIFT_DETECTED"
    assert len(rec["drifts"]) == 1
    assert any("[DRIFT]" in m for m in notifier.outbox)

    # 2. Margin ratio warning (>60%) and critical (>75% -> kill switch)
    statuses = watchdog.check_margin_ratios({"okx": 0.65, "bybit": 0.80})
    assert statuses["okx"] == "WARNING"
    assert statuses["bybit"] == "CRITICAL"
    assert redis.get("core:kill_switch") == "true"
    assert any("[WARNING]" in m for m in notifier.outbox)
    assert any("[CRITICAL]" in m for m in notifier.outbox)

    # 3. High ADL indicator (>=4) triggers early unwind
    adl_positions = [
        {
            "base_asset": "BTC",
            "qty": 0.02,
            "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT", "side": "buy", "adl_rank": 4},
            "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "side": "sell", "adl_rank": 2},
        }
    ]
    unwound = watchdog.check_adl_indicators(adl_positions)
    assert len(unwound) == 1
    assert unwound[0]["adl_rank"] == 4
    assert unwound[0]["unwind"]["status"] == "UNWOUND_VERIFIED"
    assert unwound[0]["unwind"]["verified_flat"] is True
    assert redis.get("core:kill_switch") == "true"
    assert any("[ADL WARNING]" in m for m in notifier.outbox)


def test_postgres_store_integration_with_consumer_and_watchdog():
    from db import PostgresStore

    class FakeCursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            q = query.strip()
            self.conn.queries.append((q, params))
            if "INSERT INTO active_positions" in q and params:
                self.conn.active_positions = [tuple(params)]
            elif "UPDATE active_positions" in q and "status = 'CLOSED'" in q:
                self.conn.active_positions = []

        def fetchall(self):
            last_q = self.conn.queries[-1][0] if self.conn.queries else ""
            if "FROM runtime_settings" in last_q:
                return [("funding_carry_core", True, True, 250.0, 500.0, 7.0, None, None)]
            if "FROM active_positions" in last_q:
                return list(self.conn.active_positions)
            return []

    class FakeConn:
        def __init__(self):
            self.queries = []
            self.active_positions = []

        def cursor(self):
            return FakeCursor(self)

    fake_conn = FakeConn()
    store = PostgresStore(conn=fake_conn)
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})

    now_ms = 1_700_000_000_000
    consumer = SignalConsumer(
        redis,
        executor,
        clock_ms=lambda: now_ms,
        db_store=store,
    )
    sig = make_valid_signal(now_ms=now_ms, target_notional_usdt=1000.2)
    res = consumer.process_signal(sig)
    assert res["status"] == "DRY_RUN_Simulated"
    assert res["effective_notional_usdt"] == pytest.approx(250.0)
    assert any("INSERT INTO signal_audit" in q[0] for q in fake_conn.queries)
    assert any("INSERT INTO trade_executions" in q[0] for q in fake_conn.queries)
    assert any("INSERT INTO active_positions" in q[0] for q in fake_conn.queries)

    # Duplicate open position on same base_asset is rejected
    dup_res = consumer.process_signal(sig)
    assert dup_res["status"] == "REJECTED"
    assert dup_res["reason"] == "position_already_open"

    # Watchdog loads positions from Postgres fallback when Redis core:positions is cleared
    redis.delete("core:positions")
    assert store.load_active_positions()[0]["opened_at_ms"] == now_ms
    ex_okx.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.004}]
    ex_bybit.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.001}]
    watchdog = RiskWatchdog(redis, exchanges={"okx": ex_okx, "bybit": ex_bybit}, db_store=store)
    rec = watchdog.reconcile_positions()
    assert rec["status"] == "DRIFT_DETECTED"
    assert any("INSERT INTO risk_events" in q[0] for q in fake_conn.queries)


def test_live_balance_check_ccxt_margin_adl_and_preflight():
    from main import run_preflight

    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    ex_okx.balance = {
        "free": {"USDT": 10.0},
        "used": {"USDT": 700.0},
        "total": {"USDT": 1000.0},
        "info": {"data": [{"imr": "800", "totalEq": "1000"}]},
    }
    ex_bybit.balance = {
        "free": {"USDT": 5000.0},
        "used": {"USDT": 200.0},
        "total": {"USDT": 1000.0},
        "info": {"result": {"list": [{"accountIMRate": "0.65"}]}},
    }
    ex_okx.fetch_balance = lambda: ex_okx.balance
    ex_bybit.fetch_balance = lambda: ex_bybit.balance

    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    sig = make_valid_signal(target_notional_usdt=1000.2)
    # Insufficient free USDT on leg_a (10 USDT < ~1000 USDT)
    res = executor.execute_signal(sig, dry_run=False)
    assert res["status"] == "REJECTED"
    assert res["reason"] == "insufficient_balance_leg_a"

    # Watchdog extracts margin ratio from standard CCXT fetch_balance info
    notifier = TelegramNotifier(dry_run=True)
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": ex_okx, "bybit": ex_bybit},
        notifier=notifier,
        unwind_callback=executor.unwind_route,
    )
    statuses = watchdog.check_margin_ratios()
    assert statuses["okx"] == "CRITICAL"
    assert statuses["bybit"] == "WARNING"
    assert redis.get("core:kill_switch") == "true"

    # Watchdog extracts live ADL rank from CCXT fetch_positions and marks position closed
    ex_okx.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.02, "info": {"adl": "4"}}]
    ex_bybit.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.02, "info": {"adlRankIndicator": "2"}}]
    redis.set(
        "core:positions",
        json.dumps([{"base_asset": "BTC", "qty": 0.02, "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT", "side": "buy"}, "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "side": "sell"}}]),
    )
    unwound = watchdog.check_adl_indicators()
    assert len(unwound) == 1
    assert unwound[0]["adl_rank"] == 4
    # Verified unwind finalizes the position (book closed) instead of leaving a stale row.
    assert json.loads(redis.get("core:positions")) == []

    # Auto-exit when funding spread flips negative (including perp-perp interval mismatch)
    ex_okx.fetch_funding_rate = lambda sym: {"fundingRate": 0.0002}
    ex_bybit.fetch_funding_rate = lambda sym: {"fundingRate": 0.0001}
    redis.set(
        "core:positions",
        json.dumps([{"base_asset": "ETH", "qty": 0.1, "leg_a": {"exchange": "okx", "symbol": "ETH/USDT:USDT", "side": "buy", "interval_hours": 1}, "leg_b": {"exchange": "bybit", "symbol": "ETH/USDT:USDT", "side": "sell", "interval_hours": 8}}]),
    )
    exited = watchdog.check_exit_conditions()
    assert len(exited) == 1
    assert exited[0]["base_asset"] == "ETH"
    assert json.loads(redis.get("core:positions")) == []

    # Contract multiplier (contractSize) & fetch_order fallback verification
    ex_okx.markets = {"BTC/USDT:USDT": {"contract": True, "contractSize": 0.01, "precision": {"price": 0.1, "amount": 1.0}, "limits": {"cost": {"min": 5.0}}}}
    ex_bybit.markets = {"BTC/USDT:USDT": {"contract": True, "contractSize": 1.0, "precision": {"price": 0.1, "amount": 0.001}, "limits": {"cost": {"min": 5.0}}}}
    ex_okx.balance["free"]["USDT"] = 5000.0
    ex_okx.create_order = lambda sym, otype, side, amt, price=None, params=None: {"id": "ord_okx_1", "filled": None, "status": None, "amount": amt}
    ex_okx.fetch_order = lambda oid, sym: {"id": oid, "filled": 2.0, "status": "closed"}
    res_ct = executor.execute_signal(make_valid_signal(target_notional_usdt=1000.2), dry_run=False)
    assert res_ct["status"] == "FILLED_1TO1"
    assert res_ct["qty_filled_a"] == pytest.approx(0.02)
    assert res_ct["order_a"]["amount"] == pytest.approx(2.0)

    # Preflight check passes in dry-run mode
    redis.ping = lambda: True
    assert run_preflight(redis, None, dry_run=True) is True

