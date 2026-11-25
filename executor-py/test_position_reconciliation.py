"""Tests for accurate CEX position drift reconciliation."""

from watchdog import RiskWatchdog, TelegramNotifier
from telegram_formatters import format_risk_status


class Redis:
    def get(self, key):
        return None


class PositionExchange:
    def __init__(self, positions=None, balance=None, error=None):
        self.positions = positions
        self.balance = balance or {"total": {}}
        self.error = error

    def fetch_positions(self, symbols):
        if self.error:
            raise RuntimeError(self.error)
        return self.positions

    def fetch_balance(self):
        return self.balance


def _position():
    return {
        "base_asset": "BTC",
        "qty": 0.02,
        "leg_a": {"exchange": "okx", "symbol": "BTC/USDT:USDT"},
        "leg_b": {"exchange": "mexc", "symbol": "BTC/USDT:USDT"},
    }


def test_perpetual_drift_does_not_fall_back_to_spot_wallet_balance():
    okx = PositionExchange(positions=[], balance={"total": {"BTC": 0.02}})
    mexc = PositionExchange(positions=[{"symbol": "BTC/USDT:USDT", "contracts": 0.02}])
    watchdog = RiskWatchdog(
        Redis(),
        exchanges={"okx": okx, "mexc": mexc},
        notifier=TelegramNotifier(dry_run=True),
    )

    result = watchdog.reconcile_positions([_position()])

    assert result["status"] == "DRIFT_DETECTED"
    assert result["drifts"][0]["actual_qty_a"] == 0
    assert result["drifts"][0]["leg_a_exchange"] == "okx"


def test_position_api_failure_is_reported_as_unavailable_not_as_zero_drift():
    okx = PositionExchange(error="private endpoint timeout")
    mexc = PositionExchange(positions=[{"symbol": "BTC/USDT:USDT", "contracts": 0.02}])
    notifier = TelegramNotifier(dry_run=True)
    watchdog = RiskWatchdog(
        Redis(),
        exchanges={"okx": okx, "mexc": mexc},
        notifier=notifier,
    )

    result = watchdog.reconcile_positions([_position()])

    assert result["status"] == "DATA_UNAVAILABLE"
    assert result["drifts"] == []
    assert result["unavailable"][0]["issues"][0]["exchange"] == "okx"
    assert any("okx" in message and "DRIFT DATA UNAVAILABLE" in message for message in notifier.outbox)
    dashboard = format_risk_status(result)
    assert "Drift tidak dapat diverifikasi" in dashboard
    assert "okx" in dashboard
