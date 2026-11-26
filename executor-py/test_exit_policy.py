"""Exit gating tests: profit-first closes with loss only on defined risk conditions."""

import pytest

from exit_policy import decide_exit, resolve_policy
from watchdog import RiskWatchdog, TelegramNotifier


def test_resolve_policy_defaults_and_nulls():
    policy = resolve_policy({})
    assert policy["min_profit_usdt"] == 0.0
    assert policy["max_loss_usdt"] is None
    assert policy["allow_loss_on_hold"] is False

    configured = resolve_policy({"min_profit_usdt": None, "max_loss_usdt": -0.2, "allow_loss_on_hold": True})
    assert configured["min_profit_usdt"] == 0.0
    assert configured["max_loss_usdt"] == pytest.approx(0.2)
    assert configured["allow_loss_on_hold"] is True


@pytest.mark.parametrize("pnl", [-0.01, 0.0])
def test_basis_target_waits_until_profitable(pnl):
    allowed, note = decide_exit("basis_target_reached(current=0.0001, target=0.001)", pnl, {"min_profit_usdt": 0.0})
    assert allowed is False
    assert note.startswith("profit_pending")


def test_basis_target_closes_when_profit_reached():
    reason = "basis_target_already_reached(entry=0.0005, target=0.001)"
    allowed, note = decide_exit(reason, 0.02, {"min_profit_usdt": 0.01})
    assert allowed is True
    assert note.startswith("profit")

    blocked, _ = decide_exit(reason, 0.005, {"min_profit_usdt": 0.01})
    assert blocked is False


def test_funding_flip_loss_blocked_until_cap_or_setting():
    reason = "funding_flipped(net_24h=-0.000003)"
    blocked, note = decide_exit(reason, -0.02, {"max_loss_usdt": 0.05})
    assert blocked is False
    assert "loss_blocked" in note

    allowed, note = decide_exit(reason, -0.06, {"max_loss_usdt": 0.05})
    assert allowed is True
    assert note.startswith("stop_loss")

    allowed, note = decide_exit(reason, -0.02, {"allow_loss_on_hold": True})
    assert allowed is True
    assert note == "loss_allowed_by_setting"


def test_funding_flip_requires_confirmed_samples_within_window():
    redis = type("Redis", (), {"get": lambda self, key: None, "set": lambda *args, **kwargs: True})()
    watchdog = RiskWatchdog(redis, exchanges={}, notifier=TelegramNotifier(dry_run=True))
    position = {"base_asset": "BTC", "profile_id": "funding_carry_core"}
    settings = {"funding_flip_confirm_samples": 3, "funding_flip_confirm_window_ms": 300_000}

    assert watchdog._confirm_funding_flip(position, settings, 1_000) is False
    assert watchdog._confirm_funding_flip(position, settings, 100_000) is False
    assert watchdog._confirm_funding_flip(position, settings, 200_000) is True
    assert "BTC" not in watchdog._funding_flip_confirmations


def test_funding_confirmation_resets_after_window_expiry():
    redis = type("Redis", (), {"get": lambda self, key: None, "set": lambda *args, **kwargs: True})()
    watchdog = RiskWatchdog(redis, exchanges={}, notifier=TelegramNotifier(dry_run=True))
    position = {"base_asset": "ETH", "profile_id": "funding_carry_core"}
    settings = {"funding_flip_confirm_samples": 3, "funding_flip_confirm_window_ms": 100}

    assert watchdog._confirm_funding_flip(position, settings, 1_000) is False
    assert watchdog._confirm_funding_flip(position, settings, 1_102) is False
    assert watchdog._funding_flip_confirmations["ETH"][0] == 1


def test_hold_exit_requires_profit_unless_loss_allowed():
    reason = "target_hold_days_reached"
    assert decide_exit(reason, -0.5, {"max_loss_usdt": 1.0})[0] is False
    assert decide_exit(reason, -0.5, {"max_loss_usdt": 0.1})[0] is True
    assert decide_exit(reason, 0.01, {})[0] is True


def test_risk_exits_always_allowed_and_unknown_pnl_does_not_trap():
    assert decide_exit("basis_adverse_stop(entry=0.02, current=0.03)", -5.0, {})[0] is True
    allowed, note = decide_exit("basis_target_reached(current=0.0, target=0.001)", None, {})
    assert allowed is False
    assert note == "profit_pending(pnl_unavailable)"


class TickerExchange:
    def __init__(self, long_mark, short_mark):
        self.long_mark = long_mark
        self.short_mark = short_mark

    def fetch_ticker(self, symbol):
        price = self.long_mark if ":" in symbol and symbol.startswith("LONG") else None
        return {"mark": self.long_mark if price is None else price}


def _basis_position():
    return {
        "base_asset": "BTC",
        "profile_id": "basis_convergence_fast",
        "qty": 0.1,
        "opened_at_ms": 1_700_000_000_000,
        "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT", "side": "buy", "price": 100.0},
        "leg_b": {"exchange": "mexc", "symbol": "BTC/USDT:USDT", "side": "sell", "price": 102.0},
    }


class MarkExchange:
    def __init__(self, marks):
        self.marks = marks

    def fetch_ticker(self, symbol):
        return {"mark": self.marks[symbol]}


class _Store:
    def __init__(self, settings):
        self.settings = settings
        self.closed_trades = []

    def load_profile_settings(self):
        return {"basis_convergence_fast": dict(self.settings)}

    def record_closed_trade(self, position, unwind_result, pnl, close_reason):
        self.closed_trades.append((position["base_asset"], close_reason, pnl["realized_pnl_usdt"]))


def _watchdog(settings, marks, calls):
    store = _Store(settings)
    unwind = {
        "status": "UNWOUND_VERIFIED",
        "verified_flat": True,
        "order_a": {"average": 100.0},
        "order_b": {"average": 100.0},
    }

    def callback(_pos):
        calls.append(_pos)
        return dict(unwind)

    redis = type("Redis", (), {"get": lambda self, key: None})()
    watchdog = RiskWatchdog(
        redis,
        exchanges={"okx": MarkExchange(marks), "mexc": MarkExchange(marks)},
        notifier=TelegramNotifier(dry_run=True),
        unwind_callback=callback,
        db_store=store,
    )
    return watchdog, store


def test_watchdog_holds_basis_target_position_until_profit_target_met():
    marks = {"BTC/USDT:USDT": 102.0}  # basis converges to 0 → gross PnL 0
    calls = []
    watchdog, store = _watchdog({"min_profit_usdt": 0.5, "target_hold_days": 1.0}, marks, calls)

    exited = watchdog.check_exit_conditions([_basis_position()], now_ms=1_700_000_000_000 + 1_000)

    assert exited == []
    assert calls == [], "no order may be sent while below the profit floor"
    assert store.closed_trades == []
    assert any("EXIT HELD" in msg for msg in watchdog.notifier.outbox)


def test_watchdog_closes_when_profit_floor_met():
    marks = {"BTC/USDT:USDT": 102.0}
    calls = []
    watchdog, store = _watchdog({"min_profit_usdt": -1.0, "target_hold_days": 1.0}, marks, calls)

    exited = watchdog.check_exit_conditions([_basis_position()], now_ms=1_700_000_000_000 + 1_000)

    assert exited == []
    assert calls == []
    assert store.closed_trades == []
