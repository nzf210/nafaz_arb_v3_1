"""Tests for max leg drawdown risk safety and max open positions limits."""

import json
import time
import pytest
from unittest.mock import MagicMock

from consumer import SignalConsumer
from exit_monitor import _leg_drawdown_exit, evaluate_position_exit, parse_interval_hours
from exit_policy import decide_exit
import telegram_handlers as th
from telegram_bot import TelegramBot


class MockTickerExchange:
    def __init__(self, prices):
        self.prices = prices

    def fetch_ticker(self, symbol):
        return {"mark": self.prices.get(symbol, 1.0)}


def test_parse_interval_hours():
    assert parse_interval_hours("4h") == 4
    assert parse_interval_hours("8H") == 8
    assert parse_interval_hours("1h") == 1
    assert parse_interval_hours(4) == 4
    assert parse_interval_hours("4") == 4
    assert parse_interval_hours(None, default=8) == 8
    assert parse_interval_hours("invalid", default=8) == 8


def test_leg_drawdown_exit_evaluation():
    # Long leg entry = 1.0, short leg entry = 1.0
    position = {
        "base_asset": "AKE",
        "profile_id": "funding_carry_core",
        "qty": 1000.0,
        "opened_at_ms": 1_700_000_000_000,
        "leg_a": {
            "exchange": "okx",
            "symbol": "AKE/USDT:USDT",
            "side": "buy",
            "price": 1.0,
        },
        "leg_b": {
            "exchange": "mexc",
            "symbol": "AKE/USDT:USDT",
            "side": "sell",
            "price": 1.0,
        },
    }

    # Case 1: Long leg dropped 45% (like AKE currently: 1.0 -> 0.55)
    # With max_leg_drawdown_pct = 0.60 (60%), it should NOT trigger exit!
    exchanges_45pct = {
        "okx": MockTickerExchange({"AKE/USDT:USDT": 0.55}),
        "mexc": MockTickerExchange({"AKE/USDT:USDT": 0.55}),
    }
    settings = {"max_leg_drawdown_pct": 0.60}
    reason, issue = _leg_drawdown_exit(position, settings, exchanges_45pct)
    assert reason is None
    assert issue is None

    # Case 2: Long leg dropped 65% (1.0 -> 0.35)
    # With max_leg_drawdown_pct = 0.60, it SHOULD trigger exit!
    exchanges_65pct = {
        "okx": MockTickerExchange({"AKE/USDT:USDT": 0.35}),
        "mexc": MockTickerExchange({"AKE/USDT:USDT": 0.35}),
    }
    reason, issue = _leg_drawdown_exit(position, settings, exchanges_65pct)
    assert reason is not None
    assert "leg_drawdown_exceeded" in reason
    assert "dd=65.0%" in reason
    assert "limit=60.0%" in reason

    # Gating check: should decide_exit allow this immediately as risk exit?
    allowed, note = decide_exit(reason, -5.0, settings)
    assert allowed is True
    assert "risk_exit" in note

    # Case 3: Short leg price surged 70% (1.0 -> 1.70)
    exchanges_short_loss = {
        "okx": MockTickerExchange({"AKE/USDT:USDT": 1.70}),
        "mexc": MockTickerExchange({"AKE/USDT:USDT": 1.70}),
    }
    reason, issue = _leg_drawdown_exit(position, settings, exchanges_short_loss)
    assert reason is not None
    assert "leg_drawdown_exceeded" in reason
    assert "leg_b:mexc" in reason
    assert "dd=70.0%" in reason


def test_max_open_positions_consumer_gating():
    redis_mock = MagicMock()
    # 3 active positions in Redis
    active_positions = [
        {"base_asset": "AKE", "qty": 100},
        {"base_asset": "SOXL", "qty": 0.5},
        {"base_asset": "MOVE", "qty": 500},
    ]
    redis_mock.get.side_effect = lambda k: json.dumps(active_positions) if k == "core:positions" else None

    executor_mock = MagicMock()
    consumer = SignalConsumer(
        redis_client=redis_mock,
        executor=executor_mock,
        max_open_positions=3,
        canary_max_notional_usdt=20.0,
    )

    from test_executor import make_valid_signal
    sig = make_valid_signal(now_ms=consumer.clock_ms(), target_notional_usdt=20.0, base_asset="BTC")

    res = consumer.process_signal(sig)
    assert res["status"] == "REJECTED"
    assert res["reason"] == "max_positions_reached"
    assert res["open_positions"] == 3
    assert res["max_positions"] == 3
    executor_mock.execute_signal.assert_not_called()


def test_telegram_set_max_positions_and_drawdown():
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    bot = TelegramBot(
        bot_token="test_token",
        chat_id="-1001234567890",
        allowed_chat_ids=[-1001234567890],
        allowed_user_ids=[111222],
        dry_run=True,
        redis_client=redis_mock,
        canary_cap=20.0,
        max_open_positions=3,
        max_leg_drawdown_pct=0.60,
        send_enabled=False,
    )
    consumer_mock = MagicMock()
    consumer_mock.max_open_positions = 3
    consumer_mock.profiles = {}
    bot.consumer = consumer_mock

    # Test /set max_positions 5
    th.dispatch_command(bot, -1001234567890, 111222, "/set max_positions 5")
    assert any("Batas total posisi aktif diubah menjadi <b>5 pasang</b>" in msg for msg in bot.outbox)
    assert bot.max_open_positions == 5
    assert consumer_mock.max_open_positions == 5

    # Test /set max_drawdown 60%
    th.dispatch_command(bot, -1001234567890, 111222, "/set max_drawdown 60%")
    assert any("Batas max leg drawdown diubah menjadi <b>60.0%</b>" in msg for msg in bot.outbox)
    assert bot.max_leg_drawdown_pct == pytest.approx(0.60)

    # Test /set max_drawdown 0.45
    th.dispatch_command(bot, -1001234567890, 111222, "/set max_drawdown 0.45")
    assert any("Batas max leg drawdown diubah menjadi <b>45.0%</b>" in msg for msg in bot.outbox)
    assert bot.max_leg_drawdown_pct == pytest.approx(0.45)

    # Test /settings command displays them
    bot.outbox.clear()
    th.dispatch_command(bot, -1001234567890, 111222, "/settings")
    assert any("Max Open Positions:" in msg and "5 pasang" in msg for msg in bot.outbox)
    assert any("Max Leg Drawdown:" in msg and "45.0%" in msg for msg in bot.outbox)


def test_watchdog_populates_pnl_estimates_without_exit_reason():
    """Verify that watchdog updates _latest_pnl_estimates on every cycle even when position is healthy."""
    from watchdog import RiskWatchdog
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    exchanges = {
        "okx": MagicMock(fetch_ticker=MagicMock(return_value={"last": 100.0, "mark": 100.0})),
        "mexc": MagicMock(fetch_ticker=MagicMock(return_value={"last": 100.0, "mark": 100.0})),
    }
    wd = RiskWatchdog(redis_client=redis_mock, exchanges=exchanges, dry_run=False)
    pos = {
        "base_asset": "TEST",
        "profile_id": "funding_carry_core",
        "qty": 1.0,
        "opened_at_ms": int(time.time() * 1000) - 3600_000,  # 1 hour ago (not expired)
        "leg_a": {"exchange": "okx", "symbol": "TEST/USDT:USDT", "side": "buy", "price": 100.0, "interval_hours": 8},
        "leg_b": {"exchange": "mexc", "symbol": "TEST/USDT:USDT", "side": "sell", "price": 100.0, "interval_hours": 8},
    }
    # check_exit_conditions with no exit triggered
    exited = wd.check_exit_conditions(positions=[pos])
    assert exited == []
    # But _latest_pnl_estimates MUST be populated for Telegram /positions to display!
    assert "TEST" in wd._latest_pnl_estimates
    assert "pnl" in wd._latest_pnl_estimates["TEST"]
    assert wd._latest_pnl_estimates["TEST"]["pnl"]["gross_pnl_usdt"] == pytest.approx(0.0)

