"""Unit tests for TelegramBot, interactive handlers, and formatters (v2-style)."""

import json
import pytest
from unittest.mock import MagicMock

from telegram_bot import TelegramBot
import telegram_formatters as tf
import telegram_handlers as th


class DummyRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value
        return True

    def hget(self, name, key):
        return self.store.get(f"{name}:{key}")

    def hgetall(self, name):
        prefix = f"{name}:"
        return {k[len(prefix):]: v for k, v in self.store.items() if k.startswith(prefix)}


class DummyDB:
    def __init__(self):
        self.profiles = {
            "funding_carry_core": {"enabled": True, "dry_run": True, "canary_max_notional_usdt": 20.0},
        }
        self.executions = [
            {"id": 1, "signal_id": "sig-1", "profile_id": "p1", "base_asset": "BTC", "status": "FILLED_1TO1", "dry_run": True, "qty": 0.05, "effective_notional_usdt": 1000.0, "leg_a": {}, "leg_b": {}, "created_at": "2026-01-01 12:00:00"}
        ]
        self.signals = [
            {"signal_id": "sig-1", "profile_id": "p1", "route_type": "SPOT_PERP", "base_asset": "BTC", "target_notional_usdt": 500.0, "status": "EXECUTED", "reason": None, "created_at": "2026-01-01 12:00:00"}
        ]

    def _get_conn(self):
        return True

    def load_active_positions(self):
        return [
            {
                "base_asset": "BTC",
                "profile_id": "p1",
                "qty": 0.05,
                "leg_a": {"exchange": "okx", "symbol": "BTC/USDT", "action": "buy", "price": 50000.0},
                "leg_b": {"exchange": "bybit", "symbol": "BTC/USDT:USDT", "action": "sell", "price": 50100.0},
            }
        ]

    def list_recent_executions(self, limit=10):
        return self.executions[:limit]

    def list_recent_signals(self, limit=10):
        return self.signals[:limit]

    def load_profile_settings(self):
        return self.profiles

    def sync_env_overrides(self, dry_run=None, canary_max_notional_usdt=None):
        if dry_run is not None:
            for p in self.profiles.values():
                p["dry_run"] = dry_run
        if canary_max_notional_usdt is not None:
            for p in self.profiles.values():
                p["canary_max_notional_usdt"] = canary_max_notional_usdt


@pytest.fixture
def bot_setup():
    redis = DummyRedis()
    db = DummyDB()
    executor = MagicMock()
    executor.unwind_route.return_value = {"status": "UNWOUND_VERIFIED", "verified_flat": True}
    watchdog = MagicMock()
    watchdog._load_positions.return_value = db.load_active_positions()
    watchdog.reconcile_positions.return_value = {"status": "OK", "drifts": []}
    watchdog.exchanges = {}

    bot = TelegramBot(
        bot_token="test_token",
        chat_id="-1001234567890",
        allowed_chat_ids=[-1009876543210],
        allowed_user_ids=[111222],
        dry_run=True,
        redis_client=redis,
        db_store=db,
        executor=executor,
        watchdog=watchdog,
        canary_cap=25.0,
        send_enabled=False,
    )
    return bot, db, executor, watchdog, redis


def test_auth_rules(bot_setup):
    bot, _, _, _, _ = bot_setup
    assert bot.is_chat_allowed(-1001234567890)
    assert bot.is_chat_allowed("-1001234567890")
    assert bot.is_chat_allowed(-1009876543210)
    assert not bot.is_chat_allowed(999999)

    assert bot.is_sensitive_allowed(111222)
    assert not bot.is_sensitive_allowed(333444)


def test_command_menu_and_help(bot_setup):
    bot, _, _, _, _ = bot_setup
    th.dispatch_command(bot, -1001234567890, 111222, "/menu")
    assert any("Menu Interaktif" in msg for msg in bot.outbox)

    th.dispatch_command(bot, -1001234567890, 111222, "/help")
    assert any("Panduan Perintah" in msg for msg in bot.outbox)


def test_command_status_and_positions(bot_setup):
    bot, _, _, _, _ = bot_setup
    th.dispatch_command(bot, -1001234567890, 111222, "/status")
    assert any("Status nafaz_arb v3.1" in msg for msg in bot.outbox)

    th.dispatch_command(bot, -1001234567890, 111222, "/positions")
    assert any("Posisi Aktif Executor" in msg and "BTC" in msg for msg in bot.outbox)


def test_command_history_latest_pair(bot_setup):
    bot, _, _, _, _ = bot_setup
    th.dispatch_command(bot, -1001234567890, 111222, "/history")
    assert any("Riwayat Transaksi" in msg for msg in bot.outbox)

    th.dispatch_command(bot, -1001234567890, 111222, "/latest")
    assert any("Sinyal Arbitrase Terakhir" in msg for msg in bot.outbox)

    th.dispatch_command(bot, -1001234567890, 111222, "/pair BTC")
    assert any("BTC" in msg for msg in bot.outbox)


def test_sensitive_commands_guard(bot_setup):
    bot, _, _, _, _ = bot_setup
    # Unauthorized user
    th.dispatch_command(bot, -1001234567890, 999999, "/set canary 50")
    assert any("Akses Ditolak" in msg for msg in bot.outbox)

    # Authorized user
    th.dispatch_command(bot, -1001234567890, 111222, "/set canary 50")
    assert any("Canary cap diubah" in msg for msg in bot.outbox)
    assert bot.canary_cap == 50.0


def test_killswitch_command(bot_setup):
    bot, _, _, _, redis = bot_setup
    th.dispatch_command(bot, -1001234567890, 111222, "/killswitch on")
    assert any("KILL-SWITCH DIAKTIFKAN" in msg for msg in bot.outbox)
    assert bot.is_kill_switch_active() is True

    th.dispatch_command(bot, -1001234567890, 111222, "/killswitch off")
    assert any("Kill-Switch Dimatikan" in msg for msg in bot.outbox)
    assert bot.is_kill_switch_active() is False


def test_notif_and_mute(bot_setup):
    bot, _, _, _, _ = bot_setup
    th.dispatch_command(bot, -1001234567890, 111222, "/notif trades")
    filters = bot.get_notif_filters()
    assert filters["opps"] is False
    assert filters["fills"] is True

    th.dispatch_command(bot, -1001234567890, 111222, "/mute 30m")
    assert bot._is_muted() is True

    th.dispatch_command(bot, -1001234567890, 111222, "/unmute")
    assert bot._is_muted() is False


def test_callbacks_and_force_close(bot_setup):
    bot, _, executor, watchdog, _ = bot_setup
    # Callback status
    th.dispatch_callback(bot, -1001234567890, 111222, "cb1", "status")
    assert any("Status nafaz_arb v3.1" in msg for msg in bot.outbox)

    # Callback force close prompt
    th.dispatch_callback(bot, -1001234567890, 111222, "cb2", "force_close:BTC")
    assert any("Konfirmasi Tutup Paksa" in msg for msg in bot.outbox)

    # Callback confirm force close
    bot.dry_run = False
    th.dispatch_callback(bot, -1001234567890, 111222, "cb3", "confirm_fc:BTC")
    assert any("Tutup Paksa BTC Berhasil" in msg for msg in bot.outbox)
    executor.unwind_route.assert_called_once()
    watchdog.finalize_verified_close.assert_called_once()


def test_push_notifications(bot_setup):
    bot, _, _, _, _ = bot_setup
    bot.outbox.clear()
    sig = {
        "base_asset": "ETH",
        "profile_id": "fast",
        "target_notional_usdt": 100.0,
        "legs": {
            "leg_a": {"exchange": "okx", "action": "buy", "vwap_price": 3000.0},
            "leg_b": {"exchange": "bybit", "action": "sell", "vwap_price": 3010.0},
        },
    }
    res = {"dry_run": True, "qty": 0.03, "effective_notional_usdt": 90.0}
    bot.notify_position_opened(sig, res)
    assert any("POSISI ARBITRASE TERBUKA" in msg and "ETH" in msg for msg in bot.outbox)

    bot.outbox.clear()
    bot.notify_position_closed(
        {
            "status": "UNWOUND_VERIFIED",
            "verified_flat": True,
            "realized_pnl_usdt": 1.25,
            "close_reason": "AUTO_EXIT",
        },
        "ETH",
    )
    assert any("POSISI SELESAI DITUTUP" in msg and "ETH" in msg for msg in bot.outbox)
    assert any("1.2500 USDT" in msg and "AUTO_EXIT" in msg for msg in bot.outbox)
