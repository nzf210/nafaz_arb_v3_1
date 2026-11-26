"""Structured Telegram notifier with offline outbox fallback for dry-run / testing."""

import json
import logging
import os
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

class TelegramNotifier:
    """Structured Telegram notifier with offline outbox fallback for dry-run / testing."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        dry_run: bool = True,
    ):
        self.bot_token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.dry_run = dry_run
        self.outbox: List[str] = []

    def send(self, message: str) -> bool:
        self.outbox.append(message)
        logger.info("Telegram alert: %s", message)
        if not self.bot_token or not self.chat_id:
            return True
        prefix = "[SIMULASI] " if self.dry_run and not message.startswith("[") else ""
        text = f"{prefix}{message}"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            logger.warning("Telegram HTTP send failed: %s", exc)
            return False
