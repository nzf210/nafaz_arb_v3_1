"""Production Telegram Bot for nafaz_arb v3.1: interactive polling, group auth, notifications & control."""

import html
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import telegram_formatters as tf
import telegram_handlers as th

logger = logging.getLogger(__name__)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
NOTIF_KEY = "meta:telegram_notif"
JSON_CONTENT_TYPE = "application/json"
CORE_KILL_SWITCH = "core:kill_switch"


class TelegramBot:
    """Two-way interactive Telegram bot with command polling and push notifications."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        allowed_chat_ids: Optional[List[int]] = None,
        allowed_user_ids: Optional[List[int]] = None,
        dry_run: bool = True,
        redis_client: Optional[Any] = None,
        db_store: Optional[Any] = None,
        executor: Optional[Any] = None,
        watchdog: Optional[Any] = None,
        exchanges: Optional[Dict[str, Any]] = None,
        canary_cap: float = 20.0,
        send_enabled: bool = True,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.allowed_chat_ids = allowed_chat_ids or self._parse_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
        self.allowed_user_ids = allowed_user_ids or self._parse_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
        self.api_base_url = os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/")
        self.dry_run = dry_run
        self.redis = redis_client
        self.db_store = db_store
        self.executor = executor
        self.watchdog = watchdog
        self.exchanges = exchanges or {}
        self.canary_cap = canary_cap
        self.consumer: Optional[Any] = None
        self.send_enabled = send_enabled

        self.bot_username = ""
        self.last_offset = 0
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self.muted_until = 0.0
        self.outbox: List[str] = []
        self._alert_cooldowns: Dict[str, float] = {}
        self._filters: Dict[str, bool] = {"opps": True, "rejections": True, "fills": True, "closes": True, "errors": True}
        self._load_filters_from_redis()

    @staticmethod
    def _parse_ids(raw: str) -> List[int]:
        res: List[int] = []
        for x in raw.split(","):
            val = x.strip()
            if val:
                try:
                    res.append(int(val))
                except ValueError:
                    pass
        return res

    def _load_filters_from_redis(self) -> None:
        if not self.redis:
            return
        raw = self.redis.get(NOTIF_KEY)
        if raw:
            try:
                data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                if isinstance(data, dict):
                    self._filters.update({k: bool(v) for k, v in data.items()})
            except Exception:
                pass

    def _save_filters_to_redis(self) -> None:
        if self.redis:
            try:
                self.redis.set(NOTIF_KEY, json.dumps(self._filters))
            except Exception:
                pass

    def is_chat_allowed(self, chat_id: int) -> bool:
        if not self.chat_id and not self.allowed_chat_ids:
            return False
        if str(chat_id) == str(self.chat_id):
            return True
        return chat_id in self.allowed_chat_ids

    def is_sensitive_allowed(self, user_id: int) -> bool:
        if not self.allowed_user_ids:
            return True
        return user_id in self.allowed_user_ids

    def send_message(self, chat_id: Any, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
        self.outbox.append(text)
        logger.info("Telegram outbox: %s", text[:120])
        if not self.send_enabled or not self.bot_token or not chat_id:
            return True
        url = f"{self.api_base_url}/bot{self.bot_token}/sendMessage"
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": JSON_CONTENT_TYPE})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            if "can't parse entities" in str(exc).lower():
                payload.pop("parse_mode", None)
                body = json.dumps(payload).encode("utf-8")
                req2 = urllib.request.Request(url, data=body, headers={"Content-Type": JSON_CONTENT_TYPE})
                try:
                    with urllib.request.urlopen(req2, timeout=5) as resp2:
                        return 200 <= resp2.status < 300
                except Exception:
                    pass
            logger.warning("Telegram HTTP sendMessage error: %s", exc)
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        if not self.send_enabled or not self.bot_token:
            return
        url = f"{self.api_base_url}/bot{self.bot_token}/answerCallbackQuery"
        body = json.dumps({"callback_query_id": callback_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": JSON_CONTENT_TYPE})
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                _ = resp.read()
        except Exception as exc:
            logger.debug("answerCallbackQuery failed: %s", exc)

    def set_my_commands(self) -> None:
        if not self.send_enabled or not self.bot_token:
            return
        commands = [
            {"command": "menu", "description": "📱 Buka menu interaktif"},
            {"command": "status", "description": "⚡ Status engine & bursa"},
            {"command": "positions", "description": "💼 Posisi terbuka & force close"},
            {"command": "balance", "description": "💰 Cek saldo live exchange"},
            {"command": "history", "description": "📜 10 riwayat transaksi selesai"},
            {"command": "latest", "description": "📋 10 sinyal peluang terakhir"},
            {"command": "pair", "description": "🔎 Cek koin (cth: /pair BTC)"},
            {"command": "metrics", "description": "📊 Statistik evaluasi & rejeksi"},
            {"command": "risk", "description": "🛡️ Status margin ratio & ADL"},
            {"command": "reconcile", "description": "🧾 Rekonsiliasi posisi 1:1"},
            {"command": "settings", "description": "⚙️ Parameter runtime aktif"},
            {"command": "set", "description": "📝 Ubah parameter config"},
            {"command": "notif", "description": "🔔 Filter notifikasi Telegram"},
            {"command": "mute", "description": "🔕 Bisukan alert sementara"},
            {"command": "unmute", "description": "🔔 Aktifkan kembali notifikasi"},
            {"command": "killswitch", "description": "🛑 Hentikan/lanjutkan entry"},
            {"command": "help", "description": "ℹ️ Petunjuk lengkap"},
        ]
        url = f"{self.api_base_url}/bot{self.bot_token}/setMyCommands"
        body = json.dumps({"commands": commands}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": JSON_CONTENT_TYPE})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                logger.info("Telegram setMyCommands: HTTP %d", resp.status)
        except Exception as exc:
            logger.warning("Telegram setMyCommands failed: %s", exc)

    def send(self, message: str) -> bool:
        """Compatibility method for RiskWatchdog / DeltaNeutralExecutor notifier."""
        target_id = self.chat_id
        if not target_id and self.allowed_chat_ids:
            target_id = str(self.allowed_chat_ids[0])
        return self.send_message(target_id, message)

    def broadcast(self, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        targets: List[Any] = []
        if self.chat_id:
            targets.append(self.chat_id)
        for cid in self.allowed_chat_ids:
            if str(cid) not in [str(t) for t in targets]:
                targets.append(cid)
        for t in targets:
            self.send_message(t, text, reply_markup=reply_markup)

    def notify_position_opened(self, signal: Dict[str, Any], result: Dict[str, Any]) -> None:
        if not self._filters.get("fills", True) or self._is_muted():
            return
        msg = tf.format_position_opened_alert(signal, result)
        self.broadcast(msg, reply_markup=tf.default_keyboard())

    def notify_position_closed(self, unwind_result: Dict[str, Any], base_asset: str) -> None:
        if not self._filters.get("closes", True) or self._is_muted():
            return
        msg = tf.format_position_closed_alert(unwind_result, base_asset)
        self.broadcast(msg, reply_markup=tf.default_keyboard())

    def notify_risk_alert(self, event_type: str, severity: str, details: Dict[str, Any]) -> None:
        if not self._filters.get("errors", True) or self._is_muted():
            return
        alert_key = f"{event_type}:{details.get('base_asset', 'GLOBAL')}"
        now = time.time()
        if now - self._alert_cooldowns.get(alert_key, 0) < 120.0:
            return
        self._alert_cooldowns[alert_key] = now
        mode = "SIMULASI" if self.dry_run else "LIVE"
        msg = (
            f"🚨 <b>[{mode}] RISK ALERT: {event_type} ({severity})</b>\n\n"
            f"• Detail: <code>{json.dumps(details)}</code>\n\n"
            "<i>Periksa /risk dan /positions untuk evaluasi risiko.</i>"
        )
        self.broadcast(msg, reply_markup=tf.default_keyboard())

    def _is_muted(self) -> bool:
        return time.time() < self.muted_until

    def mute(self, minutes: int = 60) -> None:
        self.muted_until = time.time() + (minutes * 60)

    def unmute(self) -> None:
        self.muted_until = 0.0

    def get_muted_until_str(self) -> str:
        if not self._is_muted():
            return ""
        return time.strftime(TIME_FORMAT, time.gmtime(self.muted_until))

    def get_notif_filters(self) -> Dict[str, bool]:
        return dict(self._filters)

    def set_notif_preset(self, preset: str) -> None:
        if preset == "trades_only":
            self._filters = {"opps": False, "rejections": False, "fills": True, "closes": True, "errors": False}
        elif preset == "all_on":
            self._filters = {"opps": True, "rejections": True, "fills": True, "closes": True, "errors": True}
        elif preset == "all_off":
            self._filters = {"opps": False, "rejections": False, "fills": False, "closes": False, "errors": False}
        self._save_filters_to_redis()

    def toggle_notif_key(self, key: str) -> None:
        if key in self._filters:
            self._filters[key] = not self._filters[key]
            self._save_filters_to_redis()

    def is_kill_switch_active(self) -> bool:
        if not self.redis:
            return False
        val = self.redis.get(CORE_KILL_SWITCH)
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        return str(val).strip().lower() in ("true", "1", "yes", "on")

    def activate_kill_switch(self, reason: str) -> None:
        if self.redis:
            self.redis.set(CORE_KILL_SWITCH, "true")
        if self.watchdog:
            self.watchdog.activate_kill_switch(reason)

    def deactivate_kill_switch(self) -> None:
        if self.redis:
            self.redis.set(CORE_KILL_SWITCH, "false")
            self.redis.set("core:kill_switch_reason", "")

    def get_system_status(self) -> Dict[str, Any]:
        pos = self.get_active_positions()
        cexs: Dict[str, str] = {}
        if self.redis:
            for name in ("okx", "bybit", "mexc"):
                st = self.redis.hget(f"meta:exchange:{name}", "status")
                if st:
                    cexs[name] = st.decode("utf-8") if isinstance(st, bytes) else str(st)
        db_ok = False
        if self.db_store:
            try:
                db_ok = bool(self.db_store._get_conn())
            except Exception:
                db_ok = False
        return {
            "dry_run": self.dry_run,
            "kill_switch": self.is_kill_switch_active(),
            "open_positions_count": len(pos),
            "canary_cap": self.canary_cap,
            "db_ok": db_ok,
            "cex_health": cexs,
            "notif_summary": "Dibisukan" if self._is_muted() else "Aktif",
        }

    def get_active_positions(self) -> List[Dict[str, Any]]:
        if self.watchdog:
            return self.watchdog._load_positions()
        if self.db_store:
            return self.db_store.load_active_positions()
        return []

    def get_live_balances(self) -> Dict[str, Dict[str, Any]]:
        res: Dict[str, Dict[str, Any]] = {}
        if self.dry_run:
            return res
        for name, client in self.exchanges.items():
            if hasattr(client, "fetch_balance"):
                try:
                    res[name] = client.fetch_balance()
                    if name == "mexc":
                        swap_bal = client.fetch_balance({"type": "swap"})
                        if isinstance(swap_bal, dict) and swap_bal.get("total", {}).get("USDT"):
                            res[f"{name} (swap)"] = swap_bal
                except Exception as exc:
                    logger.warning("Failed fetch balance %s: %s", name, exc)
        return res

    def get_recent_executions(self, limit: int = 10) -> List[Dict[str, Any]]:
        if self.db_store and hasattr(self.db_store, "list_recent_executions"):
            return self.db_store.list_recent_executions(limit)
        return []

    def get_closed_trades(self, limit: int = 10) -> List[Dict[str, Any]]:
        if self.db_store and hasattr(self.db_store, "list_closed_trades"):
            return self.db_store.list_closed_trades(limit)
        return []

    def get_pnl_summary(self) -> Dict[str, Any]:
        if self.db_store and hasattr(self.db_store, "realized_pnl_summary"):
            return self.db_store.realized_pnl_summary()
        return {}

    def get_recent_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        if self.db_store and hasattr(self.db_store, "list_recent_signals"):
            return self.db_store.list_recent_signals(limit)
        return []

    def get_rejection_stats(self) -> Dict[str, int]:
        if not self.redis:
            return {}
        try:
            raw = self.redis.hgetall("meta:rejection_stats")
            return {
                (k.decode("utf-8") if isinstance(k, bytes) else str(k)): int(v)
                for k, v in raw.items()
            }
        except Exception:
            return {}

    def get_risk_snapshot(self) -> Dict[str, Any]:
        margins: Dict[str, float] = {}
        adls: List[Dict[str, Any]] = []
        drifts: List[Dict[str, Any]] = []
        if self.watchdog:
            positions = self.get_active_positions()
            for name, client in self.watchdog.exchanges.items():
                r = self.watchdog._extract_exchange_margin_ratio(client)
                if r is not None:
                    margins[name] = r
            for pos in positions:
                adl_a = self.watchdog._extract_leg_adl_rank(pos.get("leg_a", {}))
                adl_b = self.watchdog._extract_leg_adl_rank(pos.get("leg_b", {}))
                m_adl = max(adl_a, adl_b)
                if m_adl > 0:
                    adls.append({"base_asset": pos.get("base_asset"), "adl_rank": m_adl})
            reconciliation = self.watchdog.reconcile_positions()
            return {
                "margins": margins,
                "adls": adls,
                "drifts": reconciliation.get("drifts", []),
                "unavailable": reconciliation.get("unavailable", []),
            }
        return {"margins": margins, "adls": adls, "drifts": drifts, "unavailable": []}

    def trigger_reconciliation(self) -> Dict[str, Any]:
        if self.watchdog:
            return self.watchdog.reconcile_positions()
        return {"status": "OK", "drifts": []}

    def get_profiles(self) -> Dict[str, Any]:
        if self.db_store and hasattr(self.db_store, "load_profile_settings"):
            return self.db_store.load_profile_settings()
        if self.consumer and hasattr(self.consumer, "profiles"):
            return self.consumer.profiles
        return {}

    def update_runtime_setting(self, param: str, val_str: str) -> Tuple[bool, str]:
        if param in ("canary", "canary_max_notional_usdt"):
            try:
                cap = float(val_str)
                self.canary_cap = cap
                if self.consumer:
                    self.consumer.canary_max_notional_usdt = cap
                if self.db_store:
                    self.db_store.sync_env_overrides(canary_max_notional_usdt=cap)
                return True, f"Canary cap diubah menjadi <b>${cap:.2f} USDT</b>."
            except ValueError:
                return False, "Nilai canary harus angka (cth: 50)"
        if param in ("dryrun", "dry_run"):
            val = val_str.lower() in ("true", "1", "yes", "on")
            self.dry_run = val
            if self.consumer:
                self.consumer.default_dry_run = val
            if self.db_store:
                self.db_store.sync_env_overrides(dry_run=val)
            return True, f"Dry-run global diubah menjadi <b>{val}</b>."
        return False, f"Parameter '<code>{param}</code>' tidak didukung."

    def force_close_position(self, base_asset: str) -> Dict[str, Any]:
        positions = self.get_active_positions()
        pos = next((p for p in positions if str(p.get("base_asset", "")).upper() == base_asset.upper()), None)
        if not pos:
            return {"status": "NOT_FOUND", "error": f"Posisi {base_asset} tidak ditemukan."}
        if self.executor:
            res = self.executor.unwind_route(pos, dry_run=self.dry_run)
            if self.watchdog and not self.dry_run:
                self.watchdog.finalize_verified_close(pos, res, "MANUAL_FORCE_CLOSE")
            return res
        return {"status": "NO_EXECUTOR", "error": "Executor client tidak terhubung."}

    def start(self) -> None:
        if not self.bot_token or not self.send_enabled:
            logger.info("TelegramBot: token kosong atau send_enabled=False; polling tidak dimulai.")
            return
        self.set_my_commands()
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg_poller")
        self._poll_thread.start()
        mode = "SIMULASI" if self.dry_run else "LIVE"
        self.broadcast(
            f"🚀 <b>nafaz_arb v3.1 Executor & Watchdog Aktif!</b>\n"
            f"• Mode: <code>{mode}</code>\n"
            f"• Canary Cap: <code>${self.canary_cap:.2f} USDT</code>\n\n"
            "Ketik <code>/menu</code> untuk membuka keyboard kontrol interaktif.",
            reply_markup=tf.default_keyboard(),
        )

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        logger.info("Telegram polling started (offset=%d)", self.last_offset)
        while self._running:
            try:
                updates = self._get_updates(self.last_offset)
                for u in updates:
                    up_id = u.get("update_id", 0)
                    if up_id >= self.last_offset:
                        self.last_offset = up_id + 1
                    self._handle_update(u)
            except Exception as exc:
                logger.warning("Telegram getUpdates polling error: %s", exc)
                time.sleep(3)

    def _get_updates(self, offset: int) -> List[Dict[str, Any]]:
        url = f"{self.api_base_url}/bot{self.bot_token}/getUpdates?offset={offset}&timeout=15"
        req = urllib.request.Request(url, headers={"User-Agent": "nafaz_arb_bot"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data.get("result", [])
        return []

    def _handle_update(self, u: Dict[str, Any]) -> None:
        if "callback_query" in u:
            cb = u["callback_query"]
            msg = cb.get("message") or {}
            chat_id = msg.get("chat", {}).get("id")
            user_id = cb.get("from", {}).get("id", 0)
            cb_id = cb.get("id", "")
            data = cb.get("data", "")
            if chat_id and self.is_chat_allowed(chat_id):
                th.dispatch_callback(self, chat_id, user_id, cb_id, data)
            return

        msg = u.get("message")
        if not msg:
            return
        chat_id = msg.get("chat", {}).get("id")
        user_id = msg.get("from", {}).get("id", 0)
        text = msg.get("text", "")
        if chat_id and text and self.is_chat_allowed(chat_id):
            th.dispatch_command(self, chat_id, user_id, text)
