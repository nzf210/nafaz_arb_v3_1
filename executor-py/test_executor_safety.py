"""Regression and safety unit tests for nafaz_arb v3.1 executor-py."""

import json
import pytest

from execution import (
    DeltaNeutralExecutor,
    round_price,
)
from test_executor import MockExchange, MockRedis, make_valid_signal
from watchdog import RiskWatchdog, TelegramNotifier


def test_round_price_respects_order_side():
    assert round_price(100.01, 0.1, "buy") == pytest.approx(100.0)
    assert round_price(100.01, 0.1, "sell") == pytest.approx(100.1)


def test_live_balance_check_fails_closed_when_unverifiable():
    executor_without_client = DeltaNeutralExecutor({})
    assert executor_without_client.check_sufficient_balance("missing", 10.0) is False

    class MissingBalance:
        def fetch_balance(self):
            return {"free": {}, "USDT": {}}

    class FailingBalance:
        def fetch_balance(self):
            raise RuntimeError("temporary exchange outage")

    executor = DeltaNeutralExecutor({"missing_data": MissingBalance(), "offline": FailingBalance()})
    assert executor.check_sufficient_balance("missing_data", 10.0) is False
    assert executor.check_sufficient_balance("offline", 10.0) is False


def test_perpetual_close_refuses_non_reduce_only_fallback():
    class RejectReduceOnly:
        def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            if params and params.get("reduceOnly") is True:
                raise TypeError("reduceOnly unsupported")
            pytest.fail("unsafe close without reduceOnly must never be submitted")

    executor = DeltaNeutralExecutor({"okx": RejectReduceOnly()})
    with pytest.raises(RuntimeError, match="refusing an unsafe non-reduce-only close"):
        executor._create_close_order("okx", executor.exchanges["okx"], "BTC/USDT:USDT", "sell", 0.01)


def test_unknown_order_fill_is_not_assumed_to_be_fully_filled():
    class UnknownOrder:
        def fetch_order(self, order_id, symbol):
            raise RuntimeError("order endpoint unavailable")

    executor = DeltaNeutralExecutor({})
    filled, status = executor._resolve_filled_base(
        UnknownOrder(),
        "BTC/USDT:USDT",
        {"id": "order-unknown", "filled": None, "status": None},
        0.02,
        1.0,
    )
    assert filled == 0.0
    assert status == "unknown"


def test_unwind_remains_pending_when_close_orders_are_partially_filled():
    class PartialCloseExchange(MockExchange):
        def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            order = {
                "id": f"{self.name}_partial_close",
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "amount": amount,
                "filled": amount * 0.5,
                "price": price,
                "status": "open",
            }
            self.orders.append(order)
            return order

    ex_a = PartialCloseExchange("okx")
    ex_b = PartialCloseExchange("bybit")
    ex_a.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.05}]
    ex_b.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.05}]
    executor = DeltaNeutralExecutor({"okx": ex_a, "bybit": ex_b})
    position = {
        "base_asset": "BTC",
        "qty": 0.05,
        "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT", "side": "buy"},
        "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "side": "sell"},
    }
    result = executor.unwind_route(position)
    assert result["status"] == "UNWIND_PENDING"
    assert result["verified_flat"] is False
    assert result["leg_a_verified"] is False
    assert result["leg_b_verified"] is False


def test_contract_multiplier_converts_base_qty_to_contracts():
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    ex_okx.markets = {
        "BTC/USDT:USDT": {
            "contract": True,
            "contractSize": 0.01,
            "precision": {"price": 0.1, "amount": 1.0},
            "limits": {"cost": {"min": 5.0}},
        }
    }
    ex_bybit.markets = {
        "BTC/USDT:USDT": {
            "contract": True,
            "contractSize": 1.0,
            "precision": {"price": 0.1, "amount": 0.001},
            "limits": {"cost": {"min": 5.0}},
        }
    }
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    res = executor.execute_signal(make_valid_signal(target_notional_usdt=1000.2), dry_run=False)

    assert res["status"] == "FILLED_1TO1"
    assert res["qty_filled_a"] == pytest.approx(0.02)
    assert res["qty_filled_b"] == pytest.approx(0.02)
    assert ex_okx.orders[0]["amount"] == pytest.approx(2.0)
    assert ex_bybit.orders[0]["amount"] == pytest.approx(0.02)


def test_unknown_fill_state_aborts_instead_of_assuming_hedge():
    class UnknownFillExchange(MockExchange):
        def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            order = {
                "id": f"{self.name}_unknown",
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "amount": amount,
                "filled": None,
                "price": price,
                "status": None,
            }
            self.orders.append(order)
            return order

    ex_okx = UnknownFillExchange("okx")
    ex_bybit = MockExchange("bybit")
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    res = executor.execute_signal(make_valid_signal(target_notional_usdt=1000.2), dry_run=False)

    assert res["status"] == "ABORTED_ZERO_FILL_A"
    assert res["qty_filled_a"] == 0.0
    assert len(ex_bybit.orders) == 0


def test_watchdog_keeps_position_when_unwind_unverified():
    redis = MockRedis()
    ex_okx = MockExchange("okx", fail_create=True)
    ex_bybit = MockExchange("bybit")
    notifier = TelegramNotifier(dry_run=True)
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit}, notifier=notifier.send)
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": ex_okx, "bybit": ex_bybit},
        notifier=notifier,
        unwind_callback=executor.unwind_route,
    )
    position = {
        "base_asset": "BTC",
        "qty": 0.02,
        "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT", "side": "buy", "adl_rank": 5},
        "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "side": "sell", "adl_rank": 1},
    }
    ex_okx.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.02}]
    redis.set("core:positions", json.dumps([position]))

    unwound = watchdog.check_adl_indicators()
    assert len(unwound) == 1
    assert unwound[0]["unwind"]["status"] == "UNWIND_PENDING"
    # One failed unwind retries instead of halting the whole bot immediately.
    assert redis.get("core:kill_switch") is None
    remaining = json.loads(redis.get("core:positions"))
    assert len(remaining) == 1
    assert remaining[0]["base_asset"] == "BTC"

    # Third failed attempt escalates to an auto-clearing kill-switch.
    for _ in range(2):
        attempts, _ts = watchdog._unwind_attempts["BTC"]
        watchdog._unwind_attempts["BTC"] = (attempts, 0)
        watchdog.check_adl_indicators()
    assert redis.get("core:kill_switch") == "true"
    assert str(redis.get("core:kill_switch_reason")).startswith("auto:")
    # Kill-switch does not block exits: once both legs are flat the stale position is
    # finalized and the automatic halt clears itself, resuming entries.
    ex_okx.positions = []
    ex_bybit.positions = []
    rec = watchdog.reconcile_positions()
    assert rec["repaired"][0]["action"] == "stale_flat_close"
    assert json.loads(redis.get("core:positions")) == []
    assert redis.get("core:kill_switch") == "false"


def test_canary_min_lot_missing_credentials_and_okx_posside_fallback():
    from telegram_formatters import format_latest_signals

    ex_okx = MockExchange("okx")
    ex_mexc = MockExchange("mexc")

    # 1. Missing credentials on Leg B (bybit not configured) returns missing_credentials_leg_b
    executor_no_bybit = DeltaNeutralExecutor({"okx": ex_okx})
    res_no_cred = executor_no_bybit.execute_signal(make_valid_signal(target_notional_usdt=20.0), dry_run=False, effective_notional_usdt=10.0)
    assert res_no_cred["status"] == "REJECTED"
    assert res_no_cred["reason"] == "missing_credentials_leg_b"
    assert res_no_cred["exchange"] == "bybit"

    # 2. Canary $10 below 1 contract lot returns canary_below_min_lot without crashing
    res_min_lot = executor_no_bybit.execute_signal(make_valid_signal(target_notional_usdt=1000.0), dry_run=True, effective_notional_usdt=10.0)
    assert res_min_lot["status"] == "REJECTED"
    assert res_min_lot["reason"] == "canary_below_min_lot"

    # 3. OKX Long/Short mode sCode 51000 posSide retry succeeds on entry and close
    class PosSideOKX(MockExchange):
        def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            if not params or "posSide" not in params:
                raise RuntimeError('okx {"code":"1","data":[{"sCode":"51000","sMsg":"Parameter posSide error"}]}')
            return super().create_order(symbol, order_type, side, amount, price, params)

    pos_okx = PosSideOKX("okx")
    executor_pos = DeltaNeutralExecutor(
        {"okx": pos_okx, "bybit": ex_mexc},
        guardrails={"default": {"tick_size": 0.1, "step_size": 0.0001, "min_notional": 5.0}},
    )
    res_pos = executor_pos.execute_signal(make_valid_signal(target_notional_usdt=20.0), dry_run=False, effective_notional_usdt=10.0)
    assert res_pos["status"] == "FILLED_1TO1"
    close_ord = executor_pos._create_close_order("okx", pos_okx, "BTC/USDT:USDT", "sell", 0.0001)
    assert close_ord["side"] == "sell"

    # 4. Telegram /latest formatter shows effective canary notional and exchange name
    txt = format_latest_signals([{
        "base_asset": "SAMSUNG",
        "route_type": "perp_perp_cross_cex",
        "status": "REJECTED",
        "target_notional_usdt": 1000.0,
        "effective_notional_usdt": 10.0,
        "reason": "insufficient_balance_leg_b",
        "exchange": "bybit",
    }])
    assert "$10.00" in txt
    assert "insufficient_balance_leg_b (bybit)" in txt

    # 5. Telegram /metrics formatter sorts descending, formats thousand separators, percentages, and labels
    from telegram_formatters import format_rejection_metrics
    metrics_txt = format_rejection_metrics({"low_volume": 250, "negative_gross": 750})
    assert "Total Evaluasi Ditolak:</b> <code>1,000</code>" in metrics_txt
    assert "Funding Gross &lt;= 0</b> (<code>negative_gross</code>): <b>750</b> kali (75.0%)" in metrics_txt
    assert metrics_txt.index("negative_gross") < metrics_txt.index("low_volume")


