from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Callable

from .scheduler import SchStatus

TOGGLE_FEATURES: tuple[tuple[str, str, str], ...] = (
    ('1', 'more_fish', 'More Fish'),
    ('2', 'more_treasures', 'More Treasures'),
    ('3', 'auto_daily', 'Auto Daily'),
    ('4', 'auto_sell', 'Auto Sell'),
    ('5', 'auto_update_inventory', 'Auto Update Inv'),
    ('6', 'auto_buy_baits', 'Auto Buy Baits'),
    ('7', 'fish_on_exit', 'Fish On Exit'),
)

TOGGLE_BY_KEY = {k: feat for k, feat, _ in TOGGLE_FEATURES}
TOGGLE_LABELS = {feat: label for _, feat, label in TOGGLE_FEATURES}


def fmt_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}h{m:02d}m' if h else f'{m}m{s:02d}s'


@dataclass
class ManagedAccount:
    '''Unified read/control adapter for a running account (single or multi mode).'''
    name: str
    config: object
    menu: object
    session: object
    dispatcher: object
    scheduler: object
    profile: object
    is_dead_fn: Callable[[], bool] | None = None
    error_fn: Callable[[], str] | None = None
    watchdog_recoveries: int = 0

    @property
    def dead(self) -> bool:
        if self.is_dead_fn is not None:
            try:
                return bool(self.is_dead_fn())
            except Exception:
                return False
        return False

    @property
    def error(self) -> str:
        if self.error_fn is not None:
            try:
                err = self.error_fn()
                if err:
                    return str(err)
            except Exception:
                pass
        return str(getattr(self.session, 'fatal_close', '') or '')

    def state(self) -> str:
        if self.dead:
            return 'DEAD'
        try:
            cap = getattr(self.dispatcher, 'captcha', None)
            if cap is not None and getattr(cap, 'detected', False):
                return 'CAPTCHA'
            if getattr(self.dispatcher, 'paused', False):
                return 'PAUSED'
            status = getattr(self.scheduler, 'status', None)
            if status == SchStatus.BUSY:
                return 'WAITING'
        except Exception:
            pass
        return 'FISHING'

    def snapshot(self) -> dict:
        m = self.menu
        cfg = self.config
        up = max(0.0, time() - getattr(m, 'started', time()))
        mins = up / 60.0
        hours = max(up / 3600.0, 1.0 / 3600.0)
        fish = int(getattr(m, 'rcv_fish', 0))
        rate_min = (fish / mins) if mins > 0.05 else 0.0
        catch_hr = fish / hours

        stats = getattr(m, 'session_stats', None)
        auto_sell_total, auto_sell_count = stats.snapshot() if stats else (0.0, 0)
        bal_hr = stats.balance_per_hour() if stats else None

        cd = None
        try:
            cd = getattr(self.dispatcher.cooldown, 'last', None)
        except Exception:
            cd = None

        toggles = {feat: bool(getattr(cfg, feat, False)) for _, feat, _ in TOGGLE_FEATURES}
        recent = list(reversed(list(getattr(m, 'log', []))))[:15]
        last_catch_at = float(getattr(m, 'last_catch_at', getattr(m, 'started', time())))
        last_catch_age = max(0, int(time() - last_catch_at))

        return {
            'name': self.name,
            'username': str(getattr(self.session, 'username', '') or ''),
            'state': self.state(),
            'dead': self.dead,
            'paused': bool(getattr(self.dispatcher, 'paused', False)),
            'uptime_sec': int(up),
            'uptime': fmt_uptime(up),
            'fish': fish,
            'streak': int(getattr(m, 'rcv_streak', 0)),
            'bypasses': int(getattr(m, 'rcv_bypasses', 0)),
            'rate_per_min': round(rate_min, 2),
            'catch_per_hour': round(catch_hr, 1),
            'balance': getattr(self.profile, 'balance', None) or '-',
            'level': getattr(self.profile, 'level', None) or '-',
            'balance_per_hour': round(bal_hr, 2) if bal_hr is not None else None,
            'auto_sell_total': round(auto_sell_total, 2),
            'auto_sell_count': int(auto_sell_count),
            'cooldown': round(cd, 2) if cd is not None else None,
            'queue_len': len(getattr(self.scheduler, 'queue', [])),
            'notification': str(getattr(m, 'current_notification', '') or ''),
            'last_catch_age_sec': last_catch_age,
            'watchdog_recoveries': int(self.watchdog_recoveries),
            'toggles': toggles,
            'recent_catches': [{'time': ts, 'text': txt} for ts, txt in recent],
            'logs': self.recent_logs(100),
            'error': self.error,
        }

    def recent_logs(self, limit: int = 50) -> list[dict]:
        getter = getattr(self.menu, 'get_logs', None)
        if callable(getter):
            rows = getter(limit)
        else:
            rows = list(getattr(self.menu, 'event_logs', []))[-limit:]
        return [{'time': ts, 'text': txt} for ts, txt in rows]

    def set_paused(self, paused: bool) -> tuple[bool, str]:
        if self.dead:
            return False, f'{self.name} is DEAD.'
        currently = bool(getattr(self.dispatcher, 'paused', False))
        if currently == paused:
            return True, f'{self.name} is already {"PAUSED" if paused else "FISHING"}.'
        self.dispatcher.toggle_pause()
        return True, f'{self.name} {"paused" if paused else "resumed"}.'

    def toggle_feature(self, feature_or_key: str) -> tuple[bool, str]:
        if self.dead:
            return False, f'{self.name} is DEAD.'
        key = feature_or_key.strip().lower()
        feature = TOGGLE_BY_KEY.get(key, key)
        if feature not in TOGGLE_LABELS:
            return False, f'Unknown toggle "{feature_or_key}".'
        result = self.scheduler.toggle(feature)
        if result is None:
            return False, f'Could not toggle {TOGGLE_LABELS[feature]} (check config, e.g. bait).'
        try:
            self.menu._config_list = []
        except Exception:
            pass
        return True, f'{self.name}: {TOGGLE_LABELS[feature]} -> {"ON" if result else "OFF"}'

    def trigger_action(self, action: str) -> tuple[bool, str]:
        if self.dead and action != 'save':
            return False, f'{self.name} is DEAD.'
        act = action.strip().lower()
        c = self.scheduler.commands
        if act == 'pause':
            return self.set_paused(True)
        if act == 'resume':
            return self.set_paused(False)
        if act == 'save':
            try:
                self.config.save_automation()
                return True, f'{self.name}: automation toggles saved to config.'
            except Exception as e:
                return False, f'{self.name}: failed to save config ({type(e).__name__}: {e}).'

        cmd_map = {
            'sell': ('Sell Inventory', c.sell),
            'daily': ('Claim Daily', c.daily),
            'inventory': ('Update Inventory', c.profile),
            'inv': ('Update Inventory', c.profile),
            'profile': ('Update Inventory', c.profile),
            'more_fish': ('Buy More Fish', c.morefish),
            'fishboost': ('Buy More Fish', c.morefish),
            'more_treasures': ('Buy More Treasures', c.moretreasure),
            'treasureboost': ('Buy More Treasures', c.moretreasure),
        }
        if act in cmd_map:
            label, cmd = cmd_map[act]
            ok = self.scheduler.schedule(cmd)
            if ok:
                return True, f'{self.name}: {label} scheduled.'
            return False, f'{self.name}: {label} not scheduled (busy/locked/cooldown/captcha).'
        return False, f'Unknown action "{action}".'


class ControlHub:
    '''Shared hub exposing status and control actions to Telegram Remote and Web Dashboard.'''

    def __init__(self, accounts: list[ManagedAccount]) -> None:
        self.accounts = accounts
        self.started = time()

    def resolve(self, selector: str | None = None) -> ManagedAccount | None:
        if not self.accounts:
            return None
        if not selector:
            return self.accounts[0]
        sel = selector.strip().lower()
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(self.accounts):
                return self.accounts[idx]
            return None
        for acc in self.accounts:
            if acc.name.lower() == sel or (getattr(acc.session, 'username', '') or '').lower() == sel:
                return acc
        return None

    def snapshot_all(self) -> dict:
        return {
            'uptime': fmt_uptime(time() - self.started),
            'account_count': len(self.accounts),
            'accounts': [acc.snapshot() for acc in self.accounts],
        }

