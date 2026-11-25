'''
Headless runner for hosting panels (Pterodactyl, Docker, systemd, screen-less VPS...).

No curses: every notification is printed as a plain log line and the bot is
controlled by typing commands in the panel console (type + Enter).

Enabled when ANY of these is true:
  - "--headless" is passed:  python autofishbot.py --headless
  - AFB_HEADLESS=1 environment variable
  - running inside Pterodactyl (P_SERVER_UUID is set) or stdout is not a terminal
Force the normal curses UI with "--tui" (or AFB_HEADLESS=0).
'''
from __future__ import annotations

import signal
import sys
from datetime import datetime
from os import environ
from threading import Event, Thread
from time import time

from app import MainMenu, NotificationPriority
from app.scheduler import SchStatus

FLAGS = ('--headless', '--tui')

TOGGLES = {
    '1': 'more_fish',
    '2': 'more_treasures',
    '3': 'auto_daily',
    '4': 'auto_sell',
    '5': 'auto_update_inventory',
    '6': 'auto_buy_baits',
    '7': 'fish_on_exit',
}

HELP = '''\
[*] Console commands (type + Enter):
    status          show a summary now
    pause / resume  pause or resume fishing
    1..7            toggle: 1 more fish, 2 more treasures, 3 daily, 4 sell,
                    5 update inventory, 6 buy baits, 7 fish on exit
    f / t           buy more fish / more treasures now
    d / s / i       claim daily / sell inventory / update inventory
    w               save the current toggles to the config file
    q               stop the bot (exit code 0, the panel will NOT auto-restart)
    help            show this list'''

MULTI_HELP = '''\
[*] Multi-account console commands (type + Enter):
    status          show summary for all accounts
    use <n|name>    switch active account (e.g. "use 2" or "use akun2")
    pause [all]     pause active account (or all accounts)
    resume [all]    resume active account (or all accounts)
    1..7            toggle automation on active account
    f / t           buy more fish / more treasures on active account
    d / s / i       claim daily / sell / update inventory on active account
    w               save active account toggles to its config file
    q               stop all accounts (exit code 0, panel will NOT auto-restart)
    help            show this list'''


def configure_line_buffering() -> None:
    '''Forces line-buffered stdout/stderr so Docker/Pterodactyl consoles show logs immediately.'''
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


def wants_headless(argv: list[str]) -> bool:
    '''Decides whether the headless runner should be used.'''
    if '--tui' in argv:
        return False
    if '--headless' in argv:
        return True
    flag = environ.get('AFB_HEADLESS', '').strip().lower()
    if flag in ('1', 'true', 'on', 'yes'):
        return True
    if flag in ('0', 'false', 'off', 'no'):
        return False
    return bool(environ.get('P_SERVER_UUID')) or not sys.stdout.isatty()


def strip_flags(argv: list[str]) -> None:
    '''Removes our flags so ConfigManager does not mistake them for a config name.'''
    for flag in FLAGS:
        while flag in argv:
            argv.remove(flag)


def log(message: str, prefix: str = '') -> None:
    tag = f'[{prefix}] ' if prefix else ''
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {tag}{message}', flush=True)


class HeadlessMenu(MainMenu):
    '''Same menu object the bot expects, but notifications are printed as log lines.'''
    def __init__(self, prefix: str = '', *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._log_prefix = prefix

    def notify(self, message: str, display_time: float = NotificationPriority.NORMAL,
               delimiter: str = '...', external: bool = True) -> None:
        verbose = environ.get('AFB_VERBOSE', '').lower() in ('1', 'true', 'on')
        if display_time >= NotificationPriority.NORMAL or verbose:
            log(str(message), prefix=getattr(self, '_log_prefix', ''))
        super().notify(message, display_time, delimiter, external=external)


def _fmt(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}h{m:02d}m' if h else f'{m}m{s:02d}s'


def _account_state(dispatcher, scheduler) -> str:
    try:
        cap = getattr(dispatcher, 'captcha', None)
        if cap is not None and getattr(cap, 'detected', False):
            return 'CAPTCHA'
        if dispatcher.paused:
            return 'PAUSED'
        if scheduler.status == SchStatus.BREAK:
            return 'BREAK'
        if scheduler.status == SchStatus.BUSY:
            return 'WAITING'
    except Exception:
        pass
    return 'FISHING'


def _handle_account_command(cmd: str, config, dispatcher, scheduler, prefix: str = '') -> bool:
    '''Handles single-account control commands. Returns True if handled.'''
    c = scheduler.commands
    if cmd in TOGGLES:
        feature = TOGGLES[cmd]
        result = scheduler.toggle(feature)
        if result is not None:
            log(f'[*] {feature.replace("_", " ").upper()}: {"ON" if result else "OFF"}', prefix=prefix)
        return True
    if cmd in ('f', 'buy_morefish'):
        scheduler.schedule(c.morefish)
        return True
    if cmd in ('t', 'buy_moretreasures'):
        scheduler.schedule(c.moretreasure)
        return True
    if cmd in ('d', 'daily'):
        scheduler.schedule(c.daily)
        return True
    if cmd in ('s', 'sell'):
        scheduler.schedule(c.sell)
        return True
    if cmd in ('i', 'inv', 'inventory'):
        scheduler.schedule(c.profile)
        return True
    if cmd == 'pause':
        if not dispatcher.paused:
            dispatcher.toggle_pause()
        return True
    if cmd == 'resume':
        if dispatcher.paused:
            dispatcher.toggle_pause()
        return True
    if cmd == 'w':
        config.save_automation()
        log('[*] Automation settings saved to config.', prefix=prefix)
        return True
    return False


def _install_signals(stop: Event) -> None:
    def on_signal(signum, _frame) -> None:
        log(f'[*] Signal {signum} received, stopping...')
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass


def _status_interval() -> float:
    try:
        return float(environ.get('AFB_STATUS_MIN', '10')) * 60
    except ValueError:
        return 600.0


def run_headless(menu, config, dispatcher, profile, scheduler, threads: list[Thread]) -> None:
    '''Runs until the user quits (console "q" / SIGTERM / Ctrl+C). Exits with code 1 if the bot dies.'''
    menu.prepare(config, dispatcher, profile, scheduler)
    menu.is_alive = True
    stop = Event()

    def status() -> None:
        mins = (time() - menu.started) / 60
        rate = menu.rcv_fish / mins if mins > 0.05 else 0.0
        on = lambda v: 'ON' if v else 'off'
        log(f'[STATUS] {_account_state(dispatcher, scheduler)} | fish {menu.rcv_fish} | {rate:.1f}/min | streak {menu.rcv_streak} '
            f'| bypasses {menu.rcv_bypasses} | queue {len(scheduler.queue)} | up {_fmt(time() - menu.started)}')
        log(f'[STATUS] fish boost {on(config.more_fish)} | treasure boost {on(config.more_treasures)} '
            f'| daily {on(config.auto_daily)} | sell {on(config.auto_sell)} '
            f'| inv {on(config.auto_update_inventory)} | baits {on(config.auto_buy_baits)}')

    def handle(raw: str) -> None:
        cmd = raw.strip().lower()
        if not cmd:
            return
        try:
            if _handle_account_command(cmd, config, dispatcher, scheduler):
                return
            if cmd in ('status', 'st'):
                status()
            elif cmd in ('q', 'quit', 'exit', 'stop'):
                log('[*] Stopping...')
                stop.set()
            elif cmd in ('help', 'h', '?'):
                print(HELP, flush=True)
            else:
                log(f'[!] Unknown command "{cmd}". Type "help".')
        except Exception as e:
            log(f'[E] Command "{cmd}" failed: {type(e).__name__}: {e}')

    def stdin_loop() -> None:
        while not stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:  #EOF: this host has no console input, the bot keeps running anyway
                return
            handle(line)

    _install_signals(stop)
    Thread(target=stdin_loop, daemon=True, name='Console input').start()

    interval = _status_interval()
    last_status = time()

    log('[*] Headless mode: no curses UI. Type "help" in the console for commands.')
    session = getattr(dispatcher, 'session', None)
    while not stop.is_set() and menu.is_alive:
        for thread in threads:
            if not thread.is_alive() and not getattr(session, 'fatal_close', ''):
                menu.kill()
                sys.exit(f'[E] "{thread.name}" thread exited.')
        if interval > 0 and time() - last_status >= interval:
            status()
            last_status = time()
        stop.wait(1.0)

    fatal = getattr(session, 'fatal_close', '')
    if fatal and not stop.is_set():
        #Nothing a restart can fix (bad token / dropped on every login): exit 0 so the panel does NOT restart-loop
        log(f'[E] Stopped: {fatal}. Fix the cause (see message above) and start the server again.')
        sys.exit(0)
    if not stop.is_set():  #menu was killed by something else (e.g. reconnection failed): let the panel restart us
        sys.exit('[E] Bot stopped unexpectedly (connection lost?).')
    menu.kill()


def run_headless_multi(accounts: list) -> int:
    '''Headless runner for multi.py on Pterodactyl/Docker. Returns process exit code (0 or 1).'''
    stop = Event()
    active = [0]

    def status() -> None:
        for i, acc in enumerate(accounts):
            marker = '>' if i == active[0] else ' '
            st = 'DEAD' if acc.dead else _account_state(acc.dispatcher, acc.scheduler)
            mins = (time() - acc.menu.started) / 60
            rate = acc.menu.rcv_fish / mins if mins > 0.05 else 0.0
            log(
                f'{marker} [{i + 1}:{acc.name}] {st} | fish {acc.menu.rcv_fish} | {rate:.1f}/min '
                f'| streak {acc.menu.rcv_streak} | bypasses {acc.menu.rcv_bypasses} '
                f'| up {_fmt(time() - acc.menu.started)}'
            )

    def handle(raw: str) -> None:
        parts = raw.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        arg = parts[1].lower() if len(parts) > 1 else ''
        try:
            if cmd in ('status', 'st'):
                status()
            elif cmd == 'use' and arg:
                if arg.isdigit() and 1 <= int(arg) <= len(accounts):
                    active[0] = int(arg) - 1
                    log(f'[*] Active account: {active[0] + 1}:{accounts[active[0]].name}')
                else:
                    for i, acc in enumerate(accounts):
                        if acc.name.lower() == arg:
                            active[0] = i
                            log(f'[*] Active account: {i + 1}:{acc.name}')
                            break
                    else:
                        log(f'[!] Account "{arg}" not found.')
            elif cmd in ('pause', 'resume') and arg == 'all':
                want_pause = cmd == 'pause'
                for acc in accounts:
                    if not acc.dead and acc.dispatcher.paused != want_pause:
                        acc.dispatcher.toggle_pause()
            elif cmd in ('q', 'quit', 'exit', 'stop'):
                log('[*] Stopping all accounts...')
                stop.set()
            elif cmd in ('help', 'h', '?'):
                print(MULTI_HELP, flush=True)
            else:
                acc = accounts[active[0]]
                if _handle_account_command(cmd, acc.config, acc.dispatcher, acc.scheduler, prefix=acc.name):
                    return
                log(f'[!] Unknown command "{raw.strip()}". Type "help".')
        except Exception as e:
            log(f'[E] Command "{raw.strip()}" failed: {type(e).__name__}: {e}')

    def stdin_loop() -> None:
        while not stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            handle(line)

    _install_signals(stop)
    Thread(target=stdin_loop, daemon=True, name='Console input').start()

    interval = _status_interval()
    last_status = time()
    log(f'[*] Headless multi-account mode ({len(accounts)} accounts). Active: 1:{accounts[0].name}. Type "help" for commands.')

    while not stop.is_set():
        for acc in accounts:
            if acc.dead and not acc.dead_notified:
                acc.dead_notified = True
                acc.menu.is_alive = False
                acc.menu.notify(f'[E] {acc.error or "a worker thread stopped"}', NotificationPriority.VERY_HIGH)
        if all(acc.dead or not acc.menu.is_alive for acc in accounts):
            break
        if interval > 0 and time() - last_status >= interval:
            status()
            last_status = time()
        stop.wait(1.0)

    if stop.is_set():
        return 0
    if all(bool(getattr(acc.session, 'fatal_close', '')) for acc in accounts):
        log('[E] All accounts stopped due to fatal errors (check token/permissions). Not restarting.')
        return 0
    log('[E] All accounts stopped unexpectedly.')
    return 1
