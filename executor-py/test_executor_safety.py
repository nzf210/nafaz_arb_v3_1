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
    ex_okx = MockExchange("okx")
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
    assert redis.get("core:kill_switch") == "true"
    remaining = json.loads(redis.get("core:positions"))
    assert len(remaining) == 1
    assert remaining[0]["base_asset"] == "BTC"
