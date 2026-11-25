"""Tests for actionable dual-leg execution failure diagnostics."""

from execution import DeltaNeutralExecutor
from test_executor import MockExchange, make_valid_signal


def _signal_for_mexc():
    signal = make_valid_signal(target_notional_usdt=1000.2)
    signal["legs"]["leg_b"]["exchange"] = "mexc"
    return signal


def test_unwind_alert_includes_failed_exchange_and_exchange_error():
    okx = MockExchange("okx")
    mexc = MockExchange("mexc", fail_create=True)
    alerts = []
    executor = DeltaNeutralExecutor({"okx": okx, "mexc": mexc}, notifier=alerts.append)

    result = executor.execute_signal(_signal_for_mexc())

    assert result["status"] == "EMERGENCY_UNWOUND"
    assert result["leg_b_exchange"] == "mexc"
    assert "mexc create_order timeout/failure" in result["error"]
    assert "hedge failed on BTC/USDT:USDT (mexc)" in alerts[0]
    assert "mexc create_order timeout/failure" in alerts[0]


def test_unwind_alert_explains_non_exception_zero_fill():
    class NoFillExchange(MockExchange):
        def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            order = {
                "id": f"{self.name}_no_fill",
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "amount": amount,
                "filled": 0.0,
                "price": price,
                "status": "rejected",
            }
            self.orders.append(order)
            return order

    okx = MockExchange("okx")
    mexc = NoFillExchange("mexc")
    alerts = []
    executor = DeltaNeutralExecutor(
        {"okx": okx, "mexc": mexc},
        max_leg_b_retries=0,
        notifier=alerts.append,
    )

    result = executor.execute_signal(_signal_for_mexc())

    assert result["status"] == "EMERGENCY_UNWOUND"
    assert "status=rejected" in result["error"]
    assert "filled=0.0/" in alerts[0]
    assert "reason=limit order status=rejected" in alerts[0]
