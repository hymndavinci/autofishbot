from __future__ import annotations

from os import environ
from threading import Event, Thread
from time import time

from .control import ControlHub, ManagedAccount
from .menu import NotificationPriority
from .utils import debugger

DEFAULT_WATCHDOG_SEC = 300.0  # 5 minutes without a catch while actively FISHING
DEFAULT_COOLDOWN_SEC = 180.0  # minimum 3 minutes between automatic recoveries per account


class StuckWatchdog:
    '''Detects silent/stuck sessions (no catch while actively FISHING) and triggers recovery.'''

    def __init__(
        self,
        hub: ControlHub,
        stop_event: Event | None = None,
        timeout_sec: float = DEFAULT_WATCHDOG_SEC,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        check_interval: float = 15.0,
    ) -> None:
        self.hub = hub
        self.stop_event = stop_event or Event()
        self.timeout_sec = float(timeout_sec)
        self.cooldown_sec = float(cooldown_sec)
        self.check_interval = max(1.0, float(check_interval))
        self._last_recovery: dict[str, float] = {}
        self._thread: Thread | None = None

    @classmethod
    def from_env(cls, hub: ControlHub, stop_event: Event | None = None) -> StuckWatchdog | None:
        raw = environ.get('AFB_WATCHDOG_SEC', str(int(DEFAULT_WATCHDOG_SEC))).strip()
        try:
            timeout_sec = float(raw)
        except ValueError:
            timeout_sec = DEFAULT_WATCHDOG_SEC
        if timeout_sec <= 0:
            return None
        return cls(hub=hub, stop_event=stop_event, timeout_sec=max(30.0, timeout_sec))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(target=self._loop, daemon=True, name='Watchdog')
        self._thread.start()
        print(f'[*] Anti-stuck watchdog enabled (timeout: {int(self.timeout_sec)}s).', flush=True)

    def stop(self) -> None:
        self.stop_event.set()

    def check_account(self, acc: ManagedAccount, now: float | None = None) -> bool:
        '''Checks one account and triggers recovery if stuck. Returns True if recovery ran.'''
        now = time() if now is None else now
        if acc.dead or acc.state() != 'FISHING':
            return False
        if getattr(acc.session, 'is_reconnecting', False) or getattr(acc.session, 'fatal_close', ''):
            return False

        last_catch = float(getattr(acc.menu, 'last_catch_at', getattr(acc.menu, 'started', now)))
        idle = now - last_catch
        if idle < self.timeout_sec:
            return False

        prev_recovery = self._last_recovery.get(acc.name, 0.0)
        if now - prev_recovery < self.cooldown_sec:
            return False

        self._last_recovery[acc.name] = now
        acc.menu.last_catch_at = now
        acc.watchdog_recoveries += 1

        acc.menu.notify(
            f'[!] Watchdog: no catch for {int(idle)}s, resetting button IDs and reconnecting...',
            NotificationPriority.HIGH,
        )
        try:
            msg = getattr(acc.dispatcher, 'message', None)
            if msg is not None and hasattr(msg, 'reset_ids'):
                msg.reset_ids()
        except Exception as e:
            debugger.log(e, 'StuckWatchdog - reset_ids')

        try:
            ok = acc.session.reconnect()
            if ok:
                acc.menu.notify('[*] Watchdog: reconnection succeeded.', NotificationPriority.NORMAL)
            else:
                acc.menu.notify('[!] Watchdog: reconnect attempt failed, waiting for receiver retry.', NotificationPriority.HIGH)
        except Exception as e:
            debugger.log(e, 'StuckWatchdog - reconnect')
            acc.menu.notify(f'[!] Watchdog: reconnect error ({type(e).__name__}).', NotificationPriority.HIGH)
        return True

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            if self.stop_event.wait(self.check_interval):
                break
            now = time()
            for acc in self.hub.accounts:
                if self.stop_event.is_set():
                    break
                try:
                    self.check_account(acc, now=now)
                except Exception as e:
                    debugger.log(e, 'StuckWatchdog - check')

