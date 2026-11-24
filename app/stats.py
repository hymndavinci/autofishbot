from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from time import time
from re import search, sub


@dataclass
class SessionStats:
    """Thread-safe session counters and conservative balance estimates."""
    started: float = field(default_factory=time)
    initial_balance: float | None = None
    current_balance: float | None = None
    last_balance_update: float | None = None
    balance_started_at: float | None = None
    auto_sell_total: float = 0.0
    auto_sell_count: int = 0
    pending_auto_sell: bool = False
    pending_balance: float | None = None
    pending_auto_sell_at: float | None = None
    lock: Lock = field(default_factory=Lock, repr=False)

    @staticmethod
    def parse_money(value: str) -> float | None:
        if not value:
            return None
        cleaned = sub(r"[^0-9,.-]", "", value).replace(",", "")
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return None

    def mark_auto_sell(self) -> None:
        with self.lock:
            self.pending_auto_sell = True
            self.pending_balance = self.current_balance
            self.pending_auto_sell_at = time()

    def cancel_auto_sell(self) -> None:
        with self.lock:
            self.pending_auto_sell = False
            self.pending_balance = None
            self.pending_auto_sell_at = None

    def record_balance(self, value: float) -> None:
        now = time()
        with self.lock:
            previous = self.current_balance
            if self.initial_balance is None:
                self.initial_balance = value
                self.balance_started_at = now
            if self.pending_auto_sell and self.pending_auto_sell_at and now - self.pending_auto_sell_at > 180:
                self.pending_auto_sell = False
                self.pending_balance = None
                self.pending_auto_sell_at = None
            elif self.pending_auto_sell and previous is None:
                # No pre-sale balance baseline: avoid attributing a later unrelated change.
                self.pending_auto_sell = False
                self.pending_balance = None
                self.pending_auto_sell_at = None
            elif self.pending_auto_sell and previous is not None and value > previous:
                self.auto_sell_total += value - previous
                self.auto_sell_count += 1
                self.pending_auto_sell = False
                self.pending_balance = None
                self.pending_auto_sell_at = None
            self.current_balance = value
            self.last_balance_update = now

    def record_sell_text(self, text: str) -> bool:
        """Record an explicit sale amount only while an automatic sell is pending."""
        if not text:
            return False
        lower = text.lower()
        if not any(word in lower for word in ("sold", "sell", "sale")):
            return False
        match = search(r"\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text)
        if not match:
            match = search(r"(?:sold|sale|selling).{0,50}?(?:for|worth|total(?:led)?\s*:?)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text, flags=2)
        amount = self.parse_money(match.group(1)) if match else None
        if amount is None or amount <= 0:
            return False
        with self.lock:
            if not self.pending_auto_sell:
                return False
            self.auto_sell_total += amount
            self.auto_sell_count += 1
            self.pending_auto_sell = False
            self.pending_balance = None
            self.pending_auto_sell_at = None
        return True

    def balance_per_hour(self) -> float | None:
        with self.lock:
            if self.initial_balance is None or self.current_balance is None:
                return None
            hours = (time() - (self.balance_started_at or self.started)) / 3600
            if hours <= 0:
                return 0.0
            return (self.current_balance - self.initial_balance) / hours

    def snapshot(self) -> tuple[float, int]:
        with self.lock:
            return self.auto_sell_total, self.auto_sell_count
