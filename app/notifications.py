from __future__ import annotations

from queue import Queue, Empty
from threading import Thread, Lock
from requests import post


class NotificationManager:
    """Best-effort asynchronous Telegram and Discord notification delivery."""
    def __init__(self, config, menu=None):
        self.config = config
        self.menu = menu
        self.queue = Queue()
        self.started = False
        self.start_lock = Lock()

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

    def _deliver(self, message: str):
        webhook = getattr(self.config, "discord_webhook_url", "")
        bot_token = getattr(self.config, "telegram_bot_token", "")
        chat_id = getattr(self.config, "telegram_chat_id", "")
        if webhook:
            try:
                response = post(webhook, json={"content": message}, timeout=8)
                response.raise_for_status()
            except Exception as exc:
                if self.menu:
                    self.menu.notify(f"[!] Discord notification delivery failed: {type(exc).__name__}")
        if bot_token and chat_id:
            try:
                response = post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": message},
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
