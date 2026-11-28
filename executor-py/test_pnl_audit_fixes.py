import pytest
from unittest.mock import MagicMock
from pnl import (
    get_real_leg_funding,
    calculate_position_funding,
    compute_realized_pnl,
)

def test_get_real_leg_funding_filters_pre_and_post_trade_timestamps():
    mock_exchange = MagicMock()
    mock_exchange.has = {"fetchFundingHistory": True}
    
    # Opened at 10000 ms, closed at 20000 ms, current at 22000 ms
    mock_exchange.fetch_funding_history.return_value = [
        {"id": "1", "timestamp": 5000, "amount": 0.50},    # Before open -> MUST be ignored
        {"id": "2", "timestamp": 12000, "amount": -0.05},  # During trade -> MUST be included
        {"id": "3", "timestamp": 18000, "amount": 0.10},   # During trade -> MUST be included
        {"id": "4", "timestamp": 25000, "amount": 0.20},   # After close -> MUST be ignored
    ]
    
    # Test during trade (no closed_at_ms)
    funding_open = get_real_leg_funding(
        mock_exchange,
        ex_name="mexc",
        symbol="BLUR/USDT:USDT",
        opened_at_ms=10000,
        curr_ms=22000,
        closed_at_ms=None
    )
    # Expected: -0.05 + 0.10 = 0.05 (id 2, 3)
    # Note: id 4 timestamp 25000 is > curr_ms (22000), but even without closed_at_ms,
    # timestamps < opened_at_ms (5000) are strictly ignored!
    assert funding_open == pytest.approx(0.05)

    # Test with closed_at_ms
    funding_closed = get_real_leg_funding(
        mock_exchange,
        ex_name="mexc",
        symbol="BLUR2/USDT:USDT",  # Different symbol to avoid cache
        opened_at_ms=10000,
        curr_ms=30000,
        closed_at_ms=20000
    )
    # Expected: -0.05 + 0.10 = 0.05 (id 2, 3 only)
    assert funding_closed == pytest.approx(0.05)


def test_calculate_position_funding():
    ex_a = MagicMock()
    ex_a.fetch_funding_history.return_value = [
        {"id": "1", "timestamp": 1000, "amount": 1.00},   # Ignored (< 5000)
        {"id": "2", "timestamp": 6000, "amount": -0.02},  # Included
    ]
    
    ex_b = MagicMock()
    ex_b.fetch_funding_history.return_value = [
        {"id": "10", "timestamp": 2000, "amount": 2.00},  # Ignored (< 5000)
        {"id": "20", "timestamp": 7000, "amount": 0.05},  # Included
    ]
    
    position = {
        "opened_at_ms": 5000,
        "qty": 10.0,
        "leg_a": {
            "exchange": "okx",
            "symbol": "WLFI/USDT:USDT",
            "side": "buy",
        },
        "leg_b": {
            "exchange": "mexc",
            "symbol": "WLFI/USDT:USDT",
            "side": "sell",
        },
    }
    
    total, available = calculate_position_funding(
        position=position,
        exchanges={"okx": ex_a, "mexc": ex_b},
        curr_ms=12000,
        closed_at_ms=10000,
    )
    
    assert available is True
    # -0.02 + 0.05 = 0.03
    assert total == pytest.approx(0.03)


def test_compute_realized_pnl_fallback_fees():
    position = {
        "qty": 100.0,
        "leg_a": {
            "exchange": "okx",
            "symbol": "BLUR/USDT:USDT",
            "side": "buy",
            "price": 0.2500,
            "entry_avg_price": 0.2500,
            "entry_fee": None,
        },
        "leg_b": {
            "exchange": "mexc",
            "symbol": "BLUR/USDT:USDT",
            "side": "sell",
            "price": 0.2510,
            "entry_avg_price": 0.2510,
            "entry_fee": None,
        },
    }
    
    result = {
        "verified_flat": True,
        "order_a": {
            "average": 0.2505,  # Leg A buy 0.2500 -> sell 0.2505: +0.05 USDT gross
            "filled": 100.0,
            "fee": None,
        },
        "order_b": {
            "average": 0.2508,  # Leg B sell 0.2510 -> buy 0.2508: +0.02 USDT gross
            "filled": 100.0,
            "fee": None,
        },
        "funding_usdt": 0.0,
    }
    
    # 1. With fallback_estimated_fees=False (legacy behavior preserves None)
    pnl_no_fallback = compute_realized_pnl(position, result, fallback_estimated_fees=False)
    assert pnl_no_fallback["fees_usdt"] is None
    assert pnl_no_fallback["net_pnl_usdt"] is None
    assert pnl_no_fallback["fee_status"] == "unavailable"
    assert pnl_no_fallback["realized_pnl_usdt"] == pytest.approx(0.07)  # 0.05 + 0.02
    
    # 2. With fallback_estimated_fees=True (audit fix)
    pnl_fallback = compute_realized_pnl(position, result, fallback_estimated_fees=True)
    assert pnl_fallback["fees_usdt"] is not None
    assert pnl_fallback["net_pnl_usdt"] is not None
    assert pnl_fallback["fee_status"] == "complete"
    
    # OKX taker rate = 0.0005:
    #   Entry: 100 * 0.2500 * 0.0005 = 0.0125
    #   Exit:  100 * 0.2505 * 0.0005 = 0.012525
    #   Leg A fees = 0.025025
    # MEXC taker rate = 0.0008:
    #   Entry: 100 * 0.2510 * 0.0008 = 0.02008
    #   Exit:  100 * 0.2508 * 0.0008 = 0.020064
    #   Leg B fees = 0.040144
    # Total fees = 0.065169
    # Gross = +0.07
    # Net = 0.07 - 0.065169 = +0.004831
    assert pnl_fallback["fees_usdt"] == pytest.approx(0.065169, rel=1e-3)
    assert pnl_fallback["net_pnl_usdt"] == pytest.approx(pnl_fallback["realized_pnl_usdt"] - pnl_fallback["fees_usdt"])


def test_net_pnl_loss_when_gross_smaller_than_fees():
    position = {
        "qty": 100.0,
        "leg_a": {
            "exchange": "okx",
            "symbol": "BLUR/USDT:USDT",
            "side": "buy",
            "price": 0.2500,
            "entry_avg_price": 0.2500,
        },
        "leg_b": {
            "exchange": "mexc",
            "symbol": "BLUR/USDT:USDT",
            "side": "sell",
            "price": 0.2501,
            "entry_avg_price": 0.2501,
        },
    }
    
    # Gross is only +0.0014 USDT (just like the live trade on VPS)
    result = {
        "verified_flat": True,
        "order_a": {
            "average": 0.250014,
            "filled": 100.0,
            "fee": None,
        },
        "order_b": {
            "average": 0.2501,
            "filled": 100.0,
            "fee": None,
        },
        "funding_usdt": 0.0,
    }
    
    pnl = compute_realized_pnl(position, result, fallback_estimated_fees=True)
    # Gross is +0.0014, but fees are ~0.065 USDT
    assert pnl["realized_pnl_usdt"] == pytest.approx(0.0014)
    assert pnl["fees_usdt"] > 0.05
    # Net PnL MUST BE NEGATIVE! It cannot be reported as profit!
    assert pnl["net_pnl_usdt"] < 0
