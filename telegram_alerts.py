"""
Telegram Alert System for AlpacaBot Options.

Two channels:
1. Trade Alerts → sent to "Alpaca" topic in the group (real-time buys/sells)
2. Briefings → sent to "Alpaca" topic + saved to briefings/ for the Claude bot to read

The Claude bot reads briefing files from /workspace/AlpacaBot/briefings/
and aggregates them into smart summaries when Daniel asks.
"""
import json
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("AlpacaBot")
ET = ZoneInfo("America/New_York")

TELEGRAM_API = "https://api.telegram.org/bot{token}"


class TelegramAlerts:
    def __init__(
        self,
        bot_token: str,
        group_chat_id: str,
        alerts_topic_id: int = None,
    ):
        """
        bot_token: Telegram bot token (same bot as Claude)
        group_chat_id: The Telegram group chat ID
        alerts_topic_id: message_thread_id for the "Alpaca" topic
        """
        self.bot_token = bot_token
        self.group_chat_id = group_chat_id
        self.alerts_topic_id = alerts_topic_id
        self.api_base = TELEGRAM_API.format(token=bot_token)
        self.enabled = bool(bot_token and group_chat_id)

        if not self.enabled:
            log.warning("Telegram alerts DISABLED — missing bot_token or group_chat_id")

    def send_trade_alert(self, message: str):
        """Send a real-time trade alert to the Alpaca topic."""
        if not self.enabled:
            log.info(f"[TG disabled] {message[:100]}...")
            return

        self._send_message(message, self.alerts_topic_id)

    def send_briefing(self, message: str):
        """Send a morning/afternoon briefing to the Alpaca topic."""
        if not self.enabled:
            log.info(f"[TG disabled] {message[:100]}...")
            return

        self._send_message(message, self.alerts_topic_id)

    def _send_message(self, text: str, topic_id: int = None):
        """Send a message via Telegram Bot API."""
        try:
            url = f"{self.api_base}/sendMessage"
            payload = {
                "chat_id": self.group_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if topic_id:
                payload["message_thread_id"] = topic_id

            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                # Try without HTML parse mode if it fails (formatting issues)
                payload.pop("parse_mode", None)
                resp = requests.post(url, json=payload, timeout=10)

            if resp.status_code != 200:
                log.error(f"Telegram send failed ({resp.status_code}): {resp.text[:200]}")
            else:
                log.info("Telegram message sent successfully")

        except Exception as e:
            log.error(f"Telegram send error: {e}")

    def send_error(self, error_msg: str):
        """Send an error alert — these always go through."""
        self._send_message(f"🚨 ALPACABOT ERROR\n\n{error_msg}", self.alerts_topic_id)
