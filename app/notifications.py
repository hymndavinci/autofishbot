from __future__ import annotations

from datetime import datetime, timezone
from queue import Queue, Empty
from re import sub
from threading import Thread, Lock
from time import sleep, time
from requests import post


class NotificationManager:
    """Best-effort asynchronous Telegram and Discord notification delivery."""
    # (keyword, embed title, embed color) — first match wins, most specific first
    _EMBED_STYLES = [
        ('maximum captcha regens exceeded', '🔴 Captcha Needs Manual Input', 0xE74C3C),
        ('reconnection failed', '🔴 Reconnection Failed', 0xE74C3C),
        ('manual intervention', '🔴 Manual Intervention Required', 0xE74C3C),
        ('manual input', '🔴 Captcha Needs Manual Input', 0xE74C3C),
        ('connection lost', '🔴 Connection Lost', 0xE74C3C),
        ('reconnection succeeded', '🟢 Reconnected', 0x2ECC71),
        ('connected to discord', '🟢 Connected', 0x2ECC71),
        ('captcha bypassed', '🟢 Captcha Solved', 0x2ECC71),
        ('captcha detected', '🟡 Captcha Detected', 0xF1C40F),
        ('incorrect code', '🟠 Incorrect Code', 0xE67E22),
        ('buy failed', '🟠 Buy Failed', 0xE67E22),
        ('proxy error', '🟠 Proxy Error', 0xE67E22),
        ('autofishbot stopped', '⏹️ Bot Stopped', 0x95A5A6),
        ('stopped', '🔴 Worker Stopped', 0xE74C3C),
    ]

    def __init__(self, config, menu=None):
        self.config = config
        self.menu = menu
        self.label = '' #Account label shown on external messages (set after login)
        self.queue = Queue()
        self.started = False
        self.start_lock = Lock()

    def flush(self, timeout: float = 5.0):
        '''Best-effort wait until queued notifications are delivered (or timeout).'''
        deadline = time() + timeout
        while self.queue.unfinished_tasks > 0 and time() < deadline:
            sleep(0.05)

    def start(self):
        with self.start_lock:
            if self.started:
                return
            self.started = True
            Thread(target=self._worker, daemon=True, name="External notifications").start()

    def send(self, message: str):
        if not message:
            return
        if not (
            getattr(self.config, "discord_webhook_url", "")
            or (getattr(self.config, "telegram_bot_token", "") and getattr(self.config, "telegram_chat_id", ""))
        ):
            return
        self.start()
        self.queue.put(str(message)[:1800])

    def _discord_embed(self, message: str) -> dict:
        '''Builds a color-coded Discord embed for a notification message.'''
        lowered = message.lower()
        title, color = '🔔 Notification', 0x3498DB
        for term, style_title, style_color in self._EMBED_STYLES:
            if term in lowered:
                title, color = style_title, style_color
                break
        description = sub(r'^(\[[^\]]*\]\s*)+', '', message).strip() or message
        embed = {
            'title': title,
            'description': description,
            'color': color,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'footer': {'text': 'autofishbot'},
        }
        if self.label:
            embed['author'] = {'name': self.label}
        return embed

    def _deliver(self, message: str):
        webhook = getattr(self.config, "discord_webhook_url", "")
        bot_token = getattr(self.config, "telegram_bot_token", "")
        chat_id = getattr(self.config, "telegram_chat_id", "")
        if webhook:
            try:
                try:
                    payload = {'embeds': [self._discord_embed(message)]}
                except Exception:
                    payload = {'content': message}
                response = post(webhook, json=payload, timeout=8)
                response.raise_for_status()
            except Exception as exc:
                if self.menu:
                    self.menu.notify(f"[!] Discord notification delivery failed: {type(exc).__name__}")
        if bot_token and chat_id:
            text = f'[{self.label}] {message}' if self.label else message
            try:
                response = post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=8,
                )
                response.raise_for_status()
            except Exception as exc:
                if self.menu:
                    self.menu.notify(f"[!] Telegram notification delivery failed: {type(exc).__name__}")

    def _worker(self):
        while True:
            try:
                message = self.queue.get(timeout=1)
            except Empty:
                continue
            try:
                self._deliver(message)
            finally:
                self.queue.task_done()
