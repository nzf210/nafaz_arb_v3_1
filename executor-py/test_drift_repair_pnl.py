"""Regression tests: single-leg drift repair, stale-flat close, and realized PnL history."""

import json

import pytest

from execution import DeltaNeutralExecutor
from telegram_formatters import format_history, format_pnl_summary
from watchdog import RiskWatchdog, TelegramNotifier
from test_executor import MockExchange, MockRedis


class RecordingStore:
    def __init__(self, positions=None):
        self.positions = positions or []
        self.closed = []
        self.closed_trades = []

    def load_active_positions(self):
        return self.positions

    def close_active_position(self, base_asset, realized_pnl_usdt, close_reason):
        self.closed.append((base_asset, realized_pnl_usdt, close_reason))

    def record_closed_trade(self, position, unwind_result, pnl, close_reason):
        self.closed_trades.append(
            {
                "base_asset": position["base_asset"],
                "qty": position["qty"],
                "realized_pnl_usdt": pnl["realized_pnl_usdt"],
                "fees_usdt": pnl["fees_usdt"],
                "net_pnl_usdt": pnl["net_pnl_usdt"],
                "pnl_status": pnl["pnl_status"],
                "close_reason": close_reason,
                "leg_a_exchange": position["leg_a"]["exchange"],
                "leg_b_exchange": position["leg_b"]["exchange"],
            }
        )


class SlippageExchange(MockExchange):
    """Market close fills 1% better than the reference price (nonzero PnL)."""

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = super().create_order(symbol, order_type, side, amount, price, params)
        if order_type == "market":
            order["average"] = (price or 100.0) * 0.99
        return order


def _position(qty=0.05):
    return {
        "base_asset": "BTC",
        "profile_id": "funding_carry_core",
        "qty": qty,
        "opened_at_ms": 1_700_000_000_000,
        "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT", "side": "buy", "price": 100.0},
        "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "side": "sell", "price": 100.0},
    }


def test_unwind_closes_only_the_leg_that_still_holds_exposure():
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    ex_okx.positions = []
    ex_bybit.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.05}]
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})

    result = executor.unwind_route(_position())

    assert result["status"] == "UNWOUND_VERIFIED"
    assert result["verified_flat"] is True
    assert ex_okx.orders == [], "flat leg must not be closed again"
    assert [o["side"] for o in ex_bybit.orders] == ["buy"]
    assert result["closed_qty_a"] == pytest.approx(0.0)
    assert result["closed_qty_b"] == pytest.approx(0.05)


def test_reconcile_flattens_single_leg_and_records_realized_pnl():
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = SlippageExchange("bybit")
    ex_okx.positions = []
    ex_bybit.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.05}]
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    store = RecordingStore()
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": ex_okx, "bybit": ex_bybit},
        notifier=TelegramNotifier(dry_run=True),
        unwind_callback=executor.unwind_route,
        db_store=store,
    )
    redis.set("core:positions", json.dumps([_position()]))

    rec = watchdog.reconcile_positions()

    assert rec["status"] == "REPAIRED"
    assert rec["repaired"][0]["action"] == "flatten_single_leg"
    assert json.loads(redis.get("core:positions")) == []
    assert store.closed_trades, "closed trade must land in history"
    trade = store.closed_trades[0]
    assert trade["close_reason"] == "drift_single_leg_auto_repair"
    assert trade["realized_pnl_usdt"] == pytest.approx(0.05 * (99.0 - 100.0) * -1)
    assert trade["pnl_status"] in ("complete", "partial", "unavailable")


def test_reconcile_closes_stale_flat_position_without_orders():
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    executor = DeltaNeutralExecutor({"okx": ex_okx, "bybit": ex_bybit})
    store = RecordingStore()
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": ex_okx, "bybit": ex_bybit},
        notifier=TelegramNotifier(dry_run=True),
        unwind_callback=executor.unwind_route,
        db_store=store,
    )
    redis.set("core:positions", json.dumps([_position()]))
    watchdog.notifier.outbox.clear()

    rec = watchdog.reconcile_positions()

    assert rec["status"] == "REPAIRED"
    assert rec["repaired"][0]["action"] == "stale_flat_close"
    assert ex_okx.orders == [] and ex_bybit.orders == []
    assert store.closed[0][2] == "stale_flat_auto_closed"
    assert store.closed_trades[0]["realized_pnl_usdt"] is None
    assert store.closed_trades[0]["pnl_status"] == "unavailable"
    assert any("STALE POSITION CLOSED" in m for m in watchdog.notifier.outbox)


def test_auto_kill_switch_clears_after_all_legs_flat_but_keeps_manual_halt():
    redis = MockRedis()
    watchdog = RiskWatchdog(redis, notifier=TelegramNotifier(dry_run=True))
    watchdog.activate_kill_switch("margin critical", auto=True)
    assert redis.get("core:kill_switch_reason").startswith("auto:")
    assert watchdog.maybe_clear_auto_kill_switch() is True
    assert redis.get("core:kill_switch") == "false"

    watchdog.activate_kill_switch("Telegram manual trigger")
    assert watchdog.maybe_clear_auto_kill_switch() is False
    assert redis.get("core:kill_switch") == "true"


def test_history_and_pnl_formatters_show_realized_numbers():
    trade = {
        "base_asset": "BTC",
        "qty": 0.05,
        "leg_a_exchange": "okx",
        "leg_b_exchange": "bybit",
        "realized_pnl_usdt": 1.25,
        "fees_usdt": 0.05,
        "net_pnl_usdt": 1.2,
        "pnl_status": "complete",
        "close_reason": "drift_single_leg_auto_repair",
        "closed_at": "2026-11-26 16:00:00+00",
    }
    summary = {
        "trades": 1,
        "priced_trades": 1,
        "unpriced_trades": 0,
        "realized_pnl_usdt": 1.25,
        "fees_usdt": 0.05,
        "net_pnl_usdt": 1.2,
        "wins": 1,
        "losses": 0,
        "pnl_24h_usdt": 1.25,
    }
    history = format_history([trade], summary)
    assert "Transaksi Selesai" in history
    assert "+1.2500" in history
    assert "drift_single_leg_auto_repair" in history

    pnl_text = format_pnl_summary(summary, [trade])
    assert "Ringkasan Realized PnL" in pnl_text
    assert "+1.2500" in pnl_text
    assert "+1.2000" in pnl_text


def test_dry_run_watchdog_skips_exchange_truth_repair():
    redis = MockRedis()
    ex_okx = MockExchange("okx")
    ex_bybit = MockExchange("bybit")
    store = RecordingStore()
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": ex_okx, "bybit": ex_bybit},
        notifier=TelegramNotifier(dry_run=True),
        db_store=store,
        dry_run=True,
    )
    redis.set("core:positions", json.dumps([_position()]))

    rec = watchdog.reconcile_positions()

    assert rec["status"] == "OK"
    assert store.closed == []
    assert len(json.loads(redis.get("core:positions"))) == 1
