"""Exit policy tests for basis exits, persisted holding time, and stale market data."""

import pytest

from exit_rules import basis_exit_reason, calculate_basis_spread, calculate_realized_price_pnl
from watchdog import RiskWatchdog, TelegramNotifier


def _position(profile_id="basis_convergence_fast", opened_at_ms=1_700_000_000_000):
    return {
        "base_asset": "BTC",
        "profile_id": profile_id,
        "qty": 0.1,
        "opened_at_ms": opened_at_ms,
        "leg_a": {
            "exchange": "okx",
            "symbol": "BTC/USDT:USDT",
            "side": "buy",
            "price": 100.0,
            "entry_avg_price": 100.0,
            "interval_hours": 8,
        },
        "leg_b": {
            "exchange": "mexc",
            "symbol": "BTC/USDT:USDT",
            "side": "sell",
            "price": 102.0,
            "entry_avg_price": 102.0,
            "interval_hours": 8,
        },
    }


@pytest.mark.parametrize(
    ("current_basis", "expected_reason"),
    [(0.0005, "basis_target_reached"), (0.03, "basis_adverse_stop")],
)
def test_basis_profile_exits_on_target_or_adverse_spread(current_basis, expected_reason):
    position = _position()
    assert calculate_basis_spread(position) == pytest.approx(0.02)
    assert expected_reason in basis_exit_reason(0.02, current_basis, 0.001, 0.005)


@pytest.mark.parametrize(
    ("short_mark", "expected_reason"),
    [(100.05, "basis_target_reached"), (103.0, "basis_adverse_stop")],
)
def test_watchdog_fetches_live_basis_and_applies_exit_limits(short_mark, expected_reason):
    class TickerExchange:
        def __init__(self, mark):
            self.mark = mark

        def fetch_ticker(self, symbol):
            return {"mark": self.mark}

    now_ms = 1_700_000_000_000
    position = _position(opened_at_ms=now_ms)
    redis = type("Redis", (), {"get": lambda self, key: None})()
    unwind = {
        "status": "UNWOUND_VERIFIED",
        "verified_flat": True,
        "order_a": {"average": 100.1},
        "order_b": {"average": 102.0},
    }
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": TickerExchange(100.0), "mexc": TickerExchange(short_mark)},
        notifier=TelegramNotifier(dry_run=True),
        unwind_callback=lambda _: unwind,
    )

    exited = watchdog.check_exit_conditions([position], now_ms=now_ms + 1_000)

    assert expected_reason in exited[0]["reason"]
    assert exited[0]["unwind"]["realized_pnl_usdt"] == pytest.approx(0.01)


def test_realized_price_pnl_requires_verified_average_close_prices():
    position = _position("funding_carry_core")
    result = {
        "verified_flat": True,
        "order_a": {"average": 101.0},
        "order_b": {"average": 100.0},
    }
    assert calculate_realized_price_pnl(position, result) == pytest.approx(0.3)
    assert calculate_realized_price_pnl(position, {"verified_flat": False}) is None
    assert calculate_realized_price_pnl(position, {"verified_flat": True}) is None
    position["leg_a"].pop("entry_avg_price")
    assert calculate_realized_price_pnl(position, result) is None


class FailingFundingExchange:
    def fetch_funding_rate(self, symbol):
        raise RuntimeError("funding endpoint unavailable")


def test_missing_funding_alerts_and_profile_hold_exit_remains_active():
    now_ms = 1_700_000_000_000
    position = _position("funding_carry_core", now_ms - 86_400_000)
    redis = type("Redis", (), {"get": lambda self, key: None})()
    notifier = TelegramNotifier(dry_run=True)
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": FailingFundingExchange(), "mexc": FailingFundingExchange()},
        notifier=notifier,
    )

    assert watchdog.check_exit_conditions([position], now_ms=now_ms) == []
    assert any("EXIT DATA WARNING" in message for message in notifier.outbox)
    exited = watchdog.check_exit_conditions([position], now_ms=now_ms + 8 * 86_400_000)
    assert exited[0]["reason"] == "target_hold_days_reached"


def test_invalid_funding_payload_alerts_without_closing_position():
    position = _position("funding_carry_core")
    position["leg_a"]["current_funding_rate"] = "invalid"
    notifier = TelegramNotifier(dry_run=True)
    redis = type("Redis", (), {"get": lambda self, key: None})()
    watchdog = RiskWatchdog(redis, notifier=notifier)

    assert watchdog.check_exit_conditions([position], now_ms=1_700_000_000_000) == []
    assert any("EXIT DATA WARNING" in message for message in notifier.outbox)


def test_postgres_fallback_position_uses_open_time_and_persists_pnl():
    class PositionStore:
        def __init__(self, position):
            self.position = position
            self.closed = None

        def load_active_positions(self):
            return [self.position]

        def load_profile_settings(self):
            return {"funding_carry_core": {"target_hold_days": 7.0}}

        def close_active_position(self, base_asset, realized_pnl_usdt, close_reason):
            self.closed = (base_asset, realized_pnl_usdt, close_reason)

    now_ms = 1_700_000_000_000
    position = _position("funding_carry_core", now_ms - 8 * 86_400_000)
    store = PositionStore(position)
    redis = type("Redis", (), {"get": lambda self, key: None})()
    close_result = {
        "status": "UNWOUND_VERIFIED",
        "verified_flat": True,
        "order_a": {"average": 101.0},
        "order_b": {"average": 100.0},
    }
    watchdog = RiskWatchdog(
        redis,
        notifier=TelegramNotifier(dry_run=True),
        unwind_callback=lambda _: close_result,
        db_store=store,
    )

    exited = watchdog.check_exit_conditions(now_ms=now_ms)

    assert exited[0]["reason"] == "target_hold_days_reached"
    assert exited[0]["unwind"]["realized_pnl_usdt"] == pytest.approx(0.3)
    assert store.closed[0] == "BTC"
    assert store.closed[1] == pytest.approx(0.3)
    assert store.closed[2] == "AUTO_EXIT:target_hold_days_reached"
