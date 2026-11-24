#! /usr/bin/env python3
'''
Autofishbot - multi account launcher (ONE process, ONE terminal).

Each account keeps its own config, gateway connection, scheduler, dispatcher
and menu; they are shown as tabs in a single curses screen.

Usage:
    python multi.py                          -> every *.config inside ./configs
    python multi.py akun1.config akun2.config -> only the given files

Keys:
    Tab / Shift+Tab   next / previous account
    (everything else) the normal keybinds, applied to the account being shown
    Q                 quit ALL accounts (key follows your keybinds.json 'quit')
'''
#------------------------ IMPORTS --------------------------#
from __future__ import annotations

import curses
from os import listdir
from random import uniform
from sys import argv
from threading import Thread
from time import sleep, strftime
from os import makedirs

from app import *
from app.menu import SafeScr, ModernUI, curses_init
from app.notifications import NotificationManager
from autofishbot import Receiver, Dispatcher


#------------------------- CLASSES ---------------------------#
class Account:
    '''Everything that belongs to one account (what autofishbot.py builds in __main__).'''
    def __init__(self, name: str, config, menu, session, receiver, scheduler, dispatcher) -> None:
        self.name = name
        self.config = config
        self.menu = menu
        self.session = session
        self.receiver = receiver
        self.scheduler = scheduler
        self.dispatcher = dispatcher
        self.error: str = ''
        self.threads: list[Thread] = [
            Thread(target=self._guard, args=('Receiver', receiver.run), daemon=True, name=f'{name}:Receiver'),
            Thread(target=self._guard, args=('Scheduler', scheduler.run, dispatcher), daemon=True, name=f'{name}:Scheduler'),
            Thread(target=self._guard, args=('Dispatcher', dispatcher.run), daemon=True, name=f'{name}:Dispatcher'),
        ]
        self.dead_notified = False

    def _guard(self, label: str, fn, *args) -> None:
        '''Runs a worker thread and records WHY it stopped (otherwise a dead worker is silent).'''
        try:
            fn(*args)
            reason = 'finished'
        except SystemExit as e:
            reason = f'exit: {e.code}' if e.code else 'exit'
        except BaseException as e:
            reason = f'{type(e).__name__}: {e}'
        self.error = f'{label} stopped ({reason})'[:400]
        try:
            makedirs('./dumps', exist_ok=True)
            with open('./dumps/multi_errors.log', 'a', encoding='utf-8') as f:
                f.write(f'[{strftime("%H:%M:%S")}] {self.name} - {self.error}\n')
        except Exception:
            pass

    @property
    def dead(self) -> bool:
        '''True when one of the essential threads has stopped (that account is over, others keep running).'''
        started = all(t.ident is not None for t in self.threads)
        return started and any(not t.is_alive() for t in self.threads)

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def flags(self) -> str:
        if self.dead:
            return 'DEAD'
        if self.receiver.captcha.detected:
            return 'CAPTCHA'
        if self.dispatcher.paused:
            return 'PAUSED'
        return ''


def build_account(cfg_name: str) -> Account:
    name = cfg_name[:-len('.config')] if cfg_name.endswith('.config') else cfg_name
    config = ConfigManager(config_file=cfg_name)
    menu = CompactMenu() if config.compact_mode else MainMenu()
    menu.notifier = NotificationManager(config, menu)

    session = DiscordWrapper(config=config, menu=menu, auto_connect=True)
    receiver = Receiver(session=session, config=config, menu=menu)
    scheduler = Scheduler(session=session, config=config, menu=menu, captcha=receiver.captcha)
    receiver.sch = scheduler
    dispatcher = Dispatcher(session=session, config=config, menu=menu, sch=scheduler, rcv=receiver)

    menu.prepare(config, dispatcher, receiver.profile, scheduler)
    menu.is_alive = True
    account = Account(name, config, menu, session, receiver, scheduler, dispatcher)
    account.start()
    return account


def find_configs() -> list[str]:
    if len(argv) > 1:
        return argv[1:]
    return sorted(f for f in listdir('./configs') if f.endswith('.config'))


#------------------------- UI ---------------------------#
def draw_tabs(scr: SafeScr, accounts: list[Account], active: int, width: int) -> None:
    x = 0
    for i, acc in enumerate(accounts):
        flag = acc.flags()
        label = f' {i + 1}:{acc.name}{" [" + flag + "]" if flag else ""} '
        attr = curses.A_REVERSE | curses.A_BOLD if i == active else curses.A_DIM
        if flag in ('DEAD', 'CAPTCHA') and acc.menu._colors:
            attr |= curses.A_UNDERLINE
        if x + len(label) >= width:
            break
        scr.addstr(0, x, label, attr)
        x += len(label) + 1
    hint = 'Tab: switch'
    if x + len(hint) < width:
        scr.addstr(0, width - len(hint) - 1, hint, curses.A_DIM)


def run_ui(stdscr: curses.window, accounts: list[Account]) -> None:
    scr = SafeScr(stdscr)
    curses_init(accounts[0].menu, stdscr)
    for acc in accounts[1:]:
        acc.menu._colors = accounts[0].menu._colors

    uis = {id(acc): ModernUI(acc.menu) for acc in accounts}
    quit_key = ord(accounts[0].menu.keybinds.keybinds['quit'])
    active = 0

    while True:
        #Account bookkeeping: a dead account is reported, the others keep working
        for acc in accounts:
            if acc.dead and not acc.dead_notified:
                acc.dead_notified = True
                acc.menu.is_alive = False
                acc.menu.notify(f'[E] {acc.error or "a worker thread exited"}', NotificationPriority.VERY_HIGH)
        if all(acc.dead or not acc.menu.is_alive for acc in accounts):
            return None

        try:
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            draw_tabs(scr, accounts, active, w)

            acc = accounts[active]
            body = SafeScr(stdscr.derwin(max(1, h - 1), w, 1, 0)) if h > 2 else scr
            acc.menu.get_max_size(body)
            if acc.menu.check_dimentions():
                for k, data in enumerate(acc.menu.minimum_info):
                    body.addstr(max(0, (acc.menu.y // 2) + k - 1), max(0, (acc.menu.x // 2) - len(data) // 2),
                                data[:max(1, acc.menu.x - 1)])
            else:
                uis[id(acc)].draw(body, isinstance(acc.menu, CompactMenu))

            stdscr.touchwin()
            stdscr.refresh()
            sleep(min(acc.config.refresh_rate, 0.25))
            key = stdscr.getch()

            if key == 9:                       #Tab
                active = (active + 1) % len(accounts)
            elif key == curses.KEY_BTAB:       #Shift+Tab
                active = (active - 1) % len(accounts)
            elif key == quit_key:
                return None
            elif key not in (-1, 0):
                acc.menu.check_keybinds(key, body)

        except KeyboardInterrupt:
            return None
        except Exception as e:
            accounts[active].menu._err_notification(e)
            sleep(0.3)


#------------------------ INIT --------------------------#
if __name__ == '__main__':
    print('\n[*] Starting (multi account)...')
    names = find_configs()
    if not names:
        exit('[E] No .config files found in ./configs')

    accounts: list[Account] = []
    tokens: set[str] = set()
    for i, cfg_name in enumerate(names):
        if i:
            sleep(uniform(2, 4)) #Staggered start: accounts connect one after another
        try:
            acc = build_account(cfg_name)
        except (SystemExit, Exception) as e:
            print(f'[E] Skipping "{cfg_name}": {e}')
            continue
        if acc.config.user_token in tokens:
            print(f'[!] Skipping "{cfg_name}": same token as another config (would run one account twice).')
            acc.session.disconnect()
            continue
        tokens.add(acc.config.user_token)
        accounts.append(acc)
        print(f'[*] Account "{acc.name}" ready ({len(accounts)}/{len(names)}).')

    if not accounts:
        exit('[E] No account could be started.')

    debugger.setup(any(a.config.debug for a in accounts))

    try:
        curses.wrapper(run_ui, accounts)
    finally:
        for acc in accounts:
            acc.menu.kill()
        sent = False
        for acc in accounts:
            if acc.config.fish_on_exit and acc.menu.rcv_streak > 0:
                try:
                    cmd, data = acc.scheduler.commands.worker.data
                    acc.session.request(command=cmd, parameters=data)
                    sent = True
                except Exception:
                    pass
        if sent:
            sleep(3)
        for acc in accounts:
            acc.session.disconnect()
    exit('\n[!] User exited.')
