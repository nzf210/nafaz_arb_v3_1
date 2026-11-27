"""Command and callback query handlers for nafaz_arb v3.1 Telegram bot."""

import html
import json
import logging
from typing import Any, Dict, List, Optional
import telegram_formatters as tf

logger = logging.getLogger(__name__)

DENIED_MSG = "⛔ <b>Akses Ditolak:</b> Anda tidak diizinkan menjalankan aksi sensitif ini."


def handle_menu(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    bot.send_message(chat_id, tf.format_menu(), reply_markup=tf.default_keyboard())


def handle_help(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    bot.send_message(chat_id, tf.format_help(), reply_markup=tf.default_keyboard())


def handle_status(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    info = bot.get_system_status()
    bot.send_message(chat_id, tf.format_status(info), reply_markup=tf.default_keyboard())


def handle_positions(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    positions = bot.get_active_positions()
    bot.send_message(chat_id, tf.format_positions(positions), reply_markup=tf.positions_keyboard(positions))


def handle_position_detail(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    if not args:
        bot.send_message(chat_id, "ℹ️ <i>Gunakan:</i> <code>/position &lt;ASET&gt;</code> (cth: /position BTC)")
        return
    asset = args[0].upper()
    positions = [p for p in bot.get_active_positions() if p.get("base_asset", "").upper() == asset]
    if not positions:
        bot.send_message(chat_id, f"ℹ️ <i>Tidak ditemukan posisi terbuka untuk aset <b>{asset}</b>.</i>")
        return
    bot.send_message(chat_id, tf.format_positions(positions), reply_markup=tf.positions_keyboard(positions))


def handle_balance(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    if not bot.is_sensitive_allowed(user_id):
        bot.send_message(chat_id, DENIED_MSG)
        return
    bals = bot.get_live_balances()
    bot.send_message(chat_id, tf.format_balance(bals, bot.dry_run), reply_markup=tf.default_keyboard())


def handle_history(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    trades = bot.get_closed_trades(10)
    summary = bot.get_pnl_summary()
    bot.send_message(chat_id, tf.format_history(trades, summary), reply_markup=tf.default_keyboard())

def handle_pnl(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    summary = bot.get_pnl_summary()
    recent = bot.get_closed_trades(5)
    bot.send_message(chat_id, tf.format_pnl_summary(summary, recent), reply_markup=tf.default_keyboard())


def handle_latest(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    signals = bot.get_recent_signals(10)
    bot.send_message(chat_id, tf.format_latest_signals(signals), reply_markup=tf.default_keyboard())


def handle_pair(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    if not args:
        bot.send_message(chat_id, "ℹ️ <i>Gunakan:</i> <code>/pair &lt;SIMBOL&gt;</code> (cth: /pair BTC)")
        return
    asset = args[0].upper()
    all_signals = bot.get_recent_signals(50)
    matched = [s for s in all_signals if s.get("base_asset", "").upper() == asset]
    if not matched:
        bot.send_message(chat_id, f"🔍 <i>Tidak ada riwayat sinyal terbaru untuk <b>{asset}</b>.</i>")
        return
    bot.send_message(chat_id, tf.format_latest_signals(matched[:10]), reply_markup=tf.default_keyboard())


def handle_metrics(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    stats = bot.get_rejection_stats()
    bot.send_message(chat_id, tf.format_rejection_metrics(stats), reply_markup=tf.default_keyboard())


def handle_risk(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    risk_info = bot.get_risk_snapshot()
    bot.send_message(chat_id, tf.format_risk_status(risk_info), reply_markup=tf.default_keyboard())


def handle_reconcile(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    if args and args[0].lower() == "retry":
        if not bot.is_sensitive_allowed(user_id):
            bot.send_message(chat_id, DENIED_MSG)
            return
        res = bot.trigger_reconciliation()
        st = "🟢 <b>SELESAI (OK)</b>" if res.get("status") == "OK" else "⚠️ <b>DRIFT TERDETEKSI</b>"
        bot.send_message(chat_id, f"🧾 <b>Hasil Rekonsiliasi On-Demand:</b>\n• Status: {st}\n• Drift: <code>{len(res.get('drifts', []))}</code>")
        return
    risk_info = bot.get_risk_snapshot()
    bot.send_message(chat_id, tf.format_risk_status(risk_info), reply_markup=tf.default_keyboard())


def handle_settings(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    profiles = bot.get_profiles()
    max_positions = getattr(bot, "max_open_positions", 3)
    max_drawdown = getattr(bot, "max_leg_drawdown_pct", 0.60)
    bot.send_message(
        chat_id,
        tf.format_settings(profiles, bot.dry_run, bot.canary_cap, max_positions, max_drawdown),
        reply_markup=tf.settings_keyboard(),
    )


def handle_set(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    if not bot.is_sensitive_allowed(user_id):
        bot.send_message(chat_id, DENIED_MSG)
        return
    if len(args) < 2:
        bot.send_message(
            chat_id,
            "ℹ️ <i>Gunakan:</i> <code>/set &lt;param&gt; &lt;nilai&gt;</code>\nContoh:\n"
            "• <code>/set canary 50</code>\n"
            "• <code>/set dryrun false</code>\n"
            "• <code>/set max_positions 3</code>\n"
            "• <code>/set max_drawdown 60</code>",
        )
        return
    param = args[0].lower()
    val_str = args[1]
    success, msg = bot.update_runtime_setting(param, val_str)
    prefix = "✅" if success else "❌"
    bot.send_message(chat_id, f"{prefix} {msg}", reply_markup=tf.settings_keyboard())


def handle_notif(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    if not args:
        filters = bot.get_notif_filters()
        muted_str = bot.get_muted_until_str()
        bot.send_message(chat_id, tf.format_notif_settings(filters, muted_str), reply_markup=tf.notif_keyboard(filters))
        return
    sub = args[0].lower()
    if sub in ("trades", "trades_only"):
        bot.set_notif_preset("trades_only")
    elif sub in ("all", "on"):
        bot.set_notif_preset("all_on")
    elif sub in ("pause", "off"):
        bot.set_notif_preset("all_off")
    elif sub == "toggle" and len(args) > 1:
        bot.toggle_notif_key(args[1].lower())
    filters = bot.get_notif_filters()
    bot.send_message(chat_id, tf.format_notif_settings(filters, bot.get_muted_until_str()), reply_markup=tf.notif_keyboard(filters))


def handle_mute(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    minutes = 60
    if args:
        raw = args[0].lower().rstrip("m").rstrip("h")
        try:
            m = int(raw)
            minutes = m * 60 if "h" in args[0].lower() else m
        except ValueError:
            pass
    bot.mute(minutes)
    bot.send_message(chat_id, f"🔕 <b>Alert Dibisukan</b> selama {minutes} menit.", reply_markup=tf.default_keyboard())


def handle_unmute(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    bot.unmute()
    bot.send_message(chat_id, "🔔 <b>Alert Diaktifkan Kembali</b>", reply_markup=tf.default_keyboard())


def handle_killswitch(bot: Any, chat_id: int, user_id: int, args: List[str]) -> None:
    if not bot.is_sensitive_allowed(user_id):
        bot.send_message(chat_id, DENIED_MSG)
        return
    action = args[0].lower() if args else "toggle"
    if action == "toggle":
        cur = bot.is_kill_switch_active()
        action = "off" if cur else "on"
    if action == "on":
        bot.activate_kill_switch("Telegram manual trigger")
        bot.send_message(chat_id, "🛑 <b>KILL-SWITCH DIAKTIFKAN!</b> Semua entry baru dihentikan.", reply_markup=tf.default_keyboard())
    elif action in ("off", "resume"):
        bot.deactivate_kill_switch()
        bot.send_message(chat_id, "🟢 <b>Kill-Switch Dimatikan.</b> Entry baru dilanjutkan.", reply_markup=tf.default_keyboard())
    else:
        bot.send_message(chat_id, "ℹ️ <i>Gunakan:</i> <code>/killswitch on</code> atau <code>/killswitch off</code>")


COMMAND_DISPATCH = {
    "/start": handle_menu,
    "/menu": handle_menu,
    "/help": handle_help,
    "/status": handle_status,
    "/positions": handle_positions,
    "/position": handle_position_detail,
    "/balance": handle_balance,
    "/history": handle_history,
    "/pnl": handle_pnl,
    "/latest": handle_latest,
    "/pair": handle_pair,
    "/metrics": handle_metrics,
    "/risk": handle_risk,
    "/reconcile": handle_reconcile,
    "/settings": handle_settings,
    "/config": handle_settings,
    "/set": handle_set,
    "/notif": handle_notif,
    "/mute": handle_mute,
    "/unmute": handle_unmute,
    "/killswitch": handle_killswitch,
}


def dispatch_command(bot: Any, chat_id: int, user_id: int, text: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    handler = COMMAND_DISPATCH.get(cmd)
    if handler:
        handler(bot, chat_id, user_id, args)
    elif not cmd.startswith("/"):
        as_cmd = "/" + cmd
        if as_cmd in COMMAND_DISPATCH:
            COMMAND_DISPATCH[as_cmd](bot, chat_id, user_id, args)


SIMPLE_CALLBACKS = {
    "menu": handle_menu,
    "help": handle_help,
    "status": handle_status,
    "positions": handle_positions,
    "balance": handle_balance,
    "history": handle_history,
    "latest": handle_latest,
    "metrics": handle_metrics,
    "risk": handle_risk,
    "settings": handle_settings,
    "notif": handle_notif,
}


def dispatch_callback(bot: Any, chat_id: int, user_id: int, callback_id: str, data: str) -> None:
    bot.answer_callback(callback_id, "Memproses...")
    handler = SIMPLE_CALLBACKS.get(data)
    if handler:
        handler(bot, chat_id, user_id, [])
        return
    _dispatch_action_callback(bot, chat_id, user_id, data)


def _dispatch_action_callback(bot: Any, chat_id: int, user_id: int, data: str) -> None:
    if data == "toggle_killswitch":
        handle_killswitch(bot, chat_id, user_id, ["toggle"])
    elif data.startswith("notif:"):
        _handle_notif_callback(bot, chat_id, data)
    elif data.startswith("force_close:"):
        _handle_fc_prompt(bot, chat_id, user_id, data[len("force_close:"):])
    elif data.startswith("confirm_fc:"):
        _handle_fc_exec(bot, chat_id, user_id, data[len("confirm_fc:"):])
    elif data == "cancel_fc":
        bot.send_message(chat_id, "❌ Tutup paksa dibatalkan.", reply_markup=tf.default_keyboard())


def _handle_notif_callback(bot: Any, chat_id: int, data: str) -> None:
    action = data[len("notif:"):]
    if action == "trades_only":
        bot.set_notif_preset("trades_only")
    elif action == "all_on":
        bot.set_notif_preset("all_on")
    elif action == "all_off":
        bot.set_notif_preset("all_off")
    elif action.startswith("toggle:"):
        bot.toggle_notif_key(action[len("toggle:"):])
    filters = bot.get_notif_filters()
    bot.send_message(chat_id, tf.format_notif_settings(filters, bot.get_muted_until_str()), reply_markup=tf.notif_keyboard(filters))


def _handle_fc_prompt(bot: Any, chat_id: int, user_id: int, asset: str) -> None:
    if not bot.is_sensitive_allowed(user_id):
        bot.send_message(chat_id, DENIED_MSG)
        return
    asset = asset.upper()
    bot.send_message(
        chat_id,
        f"⚠️ <b>Konfirmasi Tutup Paksa</b>\n\n"
        f"Apakah Anda yakin ingin melikuidasi & unwind posisi <b>{asset}</b> segera di kedua bursa?",
        reply_markup=tf.confirm_force_close_keyboard(asset),
    )


def _handle_fc_exec(bot: Any, chat_id: int, user_id: int, asset: str) -> None:
    if not bot.is_sensitive_allowed(user_id):
        bot.send_message(chat_id, DENIED_MSG)
        return
    asset = asset.upper()
    res = bot.force_close_position(asset)
    if res.get("status") in ("UNWOUND", "UNWOUND_VERIFIED", "OK"):
        bot.send_message(chat_id, f"✅ <b>Tutup Paksa {asset} Berhasil!</b>", reply_markup=tf.default_keyboard())
    else:
        bot.send_message(chat_id, f"❌ <b>Tutup Paksa {asset} Gagal:</b> {res.get('error', 'Unknown')}", reply_markup=tf.default_keyboard())
