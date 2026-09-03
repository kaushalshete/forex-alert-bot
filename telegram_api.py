"""Thin Telegram Bot API client using long polling.

Polling rather than webhooks: there is no always-on server to receive a push.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import config
from db import log

API_BASE = "https://api.telegram.org/bot%s"


class TelegramError(Exception):
    """Recoverable Telegram problem. Retry on the next tick."""


class Telegram:
    def __init__(self, token=None):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")

    def _call(self, method, params=None, timeout=30):
        url = (API_BASE % self.token) + "/" + method
        data = urllib.parse.urlencode(params or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise TelegramError("HTTP %s from %s: %s" % (exc.code, method, body))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError("network error calling %s: %s" % (method, exc))
        except json.JSONDecodeError as exc:
            raise TelegramError("non-JSON response from %s: %s" % (method, exc))

        if not payload.get("ok"):
            raise TelegramError("%s failed: %s" % (method, payload.get("description")))
        return payload.get("result")

    def get_updates(self, offset, timeout=0, limit=50):
        """Long poll for new messages.

        `timeout` seconds of server-side waiting costs nothing and makes the bot
        respond to commands immediately instead of on the next tick.
        """
        return self._call("getUpdates", {
            "offset": offset,
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": json.dumps(["message"]),
        }, timeout=timeout + 25)

    def send_message(self, chat_id, text):
        """Send plain text. No parse_mode on purpose: this gets read aloud."""
        return self._call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": False,
        })

    def send_safe(self, chat_id, text):
        """Send, but never let a delivery failure abort the run."""
        try:
            self.send_message(chat_id, text)
            return True
        except TelegramError as exc:
            log("Failed to send to chat %s: %s" % (chat_id, exc))
            return False
