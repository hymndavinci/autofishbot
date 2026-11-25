#------------------------ IMPORTS --------------------------#
from __future__ import annotations

if __name__ == '__main__':
    from autofishbot import Dispatcher
from . import *
from .utils import convert_time, debugger
from .scheduler import SchStatus
from .stats import SessionStats
try:
    import curses
except ImportError:
    curses = None
from time import sleep, time
from datetime import datetime
import locale
from math import ceil
from collections import deque
from threading import Lock, Thread
from os import path, environ, name as os_name
import sys
from re import sub

class SafeScr:
    '''Thin proxy around a curses window: drawing calls that fall outside the screen are
    ignored instead of raising curses.error (which used to wipe the whole frame -> blank terminal).'''
    def __init__(self, scr) -> None:
        self._scr = scr

    def __getattr__(self, name):
        return getattr(self._scr, name)

    def _safe(self, fn, *args):
        errs = (curses.error, UnicodeError) if curses is not None else (UnicodeError,)
        try:
            return fn(*args)
        except errs:
            return None

    def addstr(self, *args):
        return self._safe(self._scr.addstr, *args)

    def hline(self, *args):
        return self._safe(self._scr.hline, *args)

    def vline(self, *args):
        return self._safe(self._scr.vline, *args)
import json

#------------------------ CONSTANTS --------------------------#
MENUART = [ r' ___        _         ___  _      _     ___       _   ',
            r'/   \ _  _ | |_  ___ | __|(_) ___| |_  | _ ) ___ | |_ ',
            r'| - || || ||  _|/ _ \| _| | |(_-/|   \ | _ \/ _ \|  _|',
            r'|_|_| \_._| \__|\___/|_|  |_|/__/|_||_||___/\___/ \__|']

C_MENUART = [r' ___  ___  ___       _   ',
             r'/   \| __|| _ ) ___ | |_ ',
             r'| - || _| | _ \/ _ \|  _|',
             r'|_|_||_|  |___/\___/ \__|']

TOGGLE_KEYS = {
    ord('1'): 'more_fish',
    ord('2'): 'more_treasures',
    ord('3'): 'auto_daily',
    ord('4'): 'auto_sell',
    ord('5'): 'auto_update_inventory',
    ord('6'): 'auto_buy_baits',
    ord('7'): 'fish_on_exit',
}

DEFAULT_KEYBINDS = {
    'pause': 'p',
    'quit': 'Q',
    'sell_inventory': 's',
    'update_inventory': 'I',
    'update_leaderboards': 'L',
    'update_cosmetics': 'K',
    'buy_morefish': 'F',
    'buy_moretreasures': 'T',
    'claim_daily': 'D',
    'show_quests': 'Y',
    'show_current_inv': 'o',
    'show_charms': 'C',
    'show_buffs': 'B',
    'show_exotic_fishes': 'e',
}

MAX_NOTIFICATION_QUEUE = 40 #Hard cap so the queue can never grow without bound

class NotificationPriority:
    '''Specifies the display time of each notification prio.'''
    VERY_LOW = 1
    LOW: int = 3
    NORMAL: int = 5
    HIGH: int = 10
    VERY_HIGH: int = 30

@dataclass(slots=True)
class Keybinder:
    '''Keybinder class is responsible to load/create/validate keybinds information to be used in menus.'''
    file: str = field(default_factory=lambda: path.join(path.dirname(path.abspath(__file__)), 'keybinds.json'))
    
    keybinds: dict = field(init=False, repr=False)
    _list: list[tuple] = field(default_factory=list)
    
    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'
    
    @property
    def list(self) -> list[tuple]:
        '''Returns the list of keybinds in a tupled format: key, action (name).'''
        if self._list == []:
            for key, value in self.keybinds.items():
                action = sub('_', ' ', key).title()
                self._list.append((value, action))
        return self._list

    def loader(self) -> dict:
        '''Loads and validates the stored keybinds.json file. If invalid 
        loads default and if nonexistent create new.'''
        if not path.exists(self.file):
            with open(self.file, 'w', encoding='utf-8') as f:
                f.write(json.dumps(DEFAULT_KEYBINDS))
            self.keybinds = DEFAULT_KEYBINDS.copy()
            return True
        else:
            try:
                with open(self.file, 'r', encoding='utf-8') as f:
                    self.keybinds = json.loads(f.read())
            except json.decoder.JSONDecodeError as e:
                print(f'Menu ({self.name}) - Err: {e}')
                return False
            
            try:
                if self.keybinds.keys() == DEFAULT_KEYBINDS.keys():
                    #Key names MUST BE the same.
                    return True
                else:
                    #Invalid keybinds
                    return False
            except AttributeError as e:
                print(f'[E] Menu ({self.name}) - Err: {e}')
                return False
    
    def __post_init__(self) -> None:
        if self.loader():
            if DEFAULT_KEYBINDS != self.keybinds:
                print('[*] Custom menu keybinds loaded.')
        else:
            print('[!] Invalid keybinds file, loading default.')
            self.keybinds = DEFAULT_KEYBINDS.copy()

@dataclass(slots=True)
class BaseMenu:
    '''Core menu class, contains all basics to elaborate a new Afb menu (GUI/CLI)'''
    
    #Pointers
    config: ConfigManager = field(init=False, repr=False)
    profile: Profile = field(init=False, repr=False)
    dispatcher: object = field(init=False, repr=False)
    sch: Scheduler = field(init=False, repr=False)
    notifier: object = field(default=None, repr=False)
    session_stats: SessionStats = field(default_factory=SessionStats, repr=False)

    #Attributes
    items: list[str] = field(default_factory=list)
    current_notification: str = ''
    notification_queue: list[tuple] = field(default_factory=list)
    keybinds: Keybinder = field(default_factory=Keybinder)
    
    #Dimentions
    x: int = 0
    y: int = 0
    minimum: tuple = (20, 5)
    
    #Flags
    autorun: bool = False
    is_alive: bool = False
    
    #Counters
    rcv_streak: int = 0
    rcv_fish: int = 0
    rcv_bypasses: int = 0

    #UI state
    started: float = field(default_factory=time)
    last_catch_at: float = field(default_factory=time)
    log: list = field(default_factory=list, repr=False)
    event_logs: deque = field(default_factory=lambda: deque(maxlen=200), repr=False)
    _unicode: bool = True
    _colors: bool = False
        
    #Backend
    _config_list: list[tuple] = field(default_factory=list)
    _notif_lock: Lock = field(default_factory=Lock, repr=False)
    
    def __post_init__(self) -> None:
        if self.autorun:
            self.run()
    
    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'
    
    @property
    def app_list(self) -> list[tuple]:
        '''Constructs and returns the content (in list format) of the app status - like counters and 
        countdowns - ready to be displayed.'''
        cd = self.dispatcher.cooldown.last
        sch_message = f'{self.sch.status.name} ({len(self.sch.queue)})'
        dsp_message = 'PAUSED' if self.dispatcher.paused else 'FISHING'
        
        if self.sch.status == SchStatus.BREAK:
            sch_message = f'{self.sch.status.name} ({round(self.sch._break_remaining, 1)}s)'
            dsp_message = 'SCH PAUSE'
        elif self.sch.status == SchStatus.BUSY:
            dsp_message = 'WAITING'

        return [
            ('STREAK', self.rcv_streak),
            ('FISH MSGS', self.rcv_fish),
            ('BYPASSES', self.rcv_bypasses),
            ('COOLDOWN', round(cd, 4) if cd else ''),
            ('SCHEDULER', sch_message),
            ('DISPATCHER', dsp_message)
        ]
    
    @property
    def config_list(self) -> list[tuple]:
        '''Constructs and returns the content (in list format) of the config options (related to automations)
        ready to be displayed.'''
        if self._config_list == []:
            def switcher(val: bool) -> str:
                return 'ON' if val else 'OFF'

            self._config_list = [
                ('[1] MORE FISH', switcher(self.config.more_fish)),
                ('[2] MORE TREASURES', switcher(self.config.more_treasures)),
                ('[3] AUTO DAILY', switcher(self.config.auto_daily)),
                ('[4] AUTO SELL', switcher(self.config.auto_sell)),
                ('[5] AUTO UPDATE INV', switcher(self.config.auto_update_inventory)),
                ('[6] AUTO BUY BAITS', switcher(self.config.auto_buy_baits)),
                ('[7] FISH ON EXIT', switcher(self.config.fish_on_exit)),
            ]
        return self._config_list
    
    @property
    def minimum_info(self) -> list[str]:
        '''Returns the content (in list format) to be displayed if current size < minimum size.'''
        return [
            f'[{self.name.upper()}]', 
            f'Mininum: {self.minimum[0]} x {self.minimum[1]} ', 
            f'Current: {self.x} x {self.y}'
        ]
    
    @property
    def notification(self) -> str:
        '''Returns the current notification ready to be displayed.'''
        quantity = len(self.notification_queue)
        message = self.current_notification if self.current_notification else 'Nothing new.'
        return f'({quantity}): {message}'
    
    #------------------------ INIT AND EXIT --------------------------#
    def prepare(self, config: ConfigManager, dispatcher: Dispatcher, profile: Profile, scheduler: Scheduler) -> None:
        '''Sets the pointers, detects unicode support and starts the notifications thread (no curses yet).'''
        #Setup class pointers.
        self.config = config
        self.dispatcher = dispatcher
        self.profile = profile
        self.sch = scheduler
        
        notifications_server = Thread(target=self.notifications_thread, daemon=True, name='Notifications server')
        notifications_server.start()

        try:
            locale.setlocale(locale.LC_ALL, '')
        except locale.Error:
            pass
        try:
            enc = (locale.getpreferredencoding(False) or '').lower()
        except Exception:
            enc = ''
        env = ' '.join(environ.get(k, '') for k in ('LC_ALL', 'LC_CTYPE', 'LANG')).lower().strip()
        self._unicode = os_name == 'nt' or (enc.startswith('utf') and ('utf' in env or not env))
        if environ.get('AFB_ASCII'):
            self._unicode = False
        elif environ.get('AFB_UNICODE'):
            self._unicode = True


    def run(self, config: ConfigManager, dispatcher: Dispatcher, profile: Profile, scheduler: Scheduler, 
            threads: list[Thread]) -> None:
        '''Prepares the menu and starts the curses interface.'''
        if curses is None:
            exit('[E] curses is not available on this platform. Install windows-curses or run with --headless.')
        self.prepare(config, dispatcher, profile, scheduler)
        try:
            curses.wrapper(lambda scr, th: self.__run__(SafeScr(scr), th), threads)
        except curses.error as e:
            exit(f'[E] {self.name} err: {e}')
    
    def log_catch(self, items: list) -> None:
        '''Stores a short one-line summary of the last catch for the RECENT panel.'''
        self.last_catch_at = time()
        parts = [sub(r'\s{2,}', ' ', sub(r':\w+:', '', str(i))).strip() for i in items if i and str(i).strip() and 'LEVEL UP' not in str(i)]
        if parts:
            with self._notif_lock:
                self.log.append((datetime.now().strftime('%H:%M:%S'), ', '.join(parts)))
                if len(self.log) > 60:
                    self.log = self.log[-60:]

    def get_logs(self, limit: int = 50) -> list[tuple[str, str]]:
        '''Returns up to the latest `limit` notification/event log lines (oldest -> newest).'''
        limit = max(1, min(int(limit), 200))
        with self._notif_lock:
            return list(self.event_logs)[-limit:]

    def kill(self) -> None:
        '''Kills the menu activity.'''
        self.is_alive = False
        
    #------------------------ DIMENTIONS --------------------------# 
    def check_dimentions(self) -> bool:
        '''Checks screen dimentions, if current size < minimum size, returns True'''
        if self.x < self.minimum[0] or self.y < self.minimum[1]:
            #print('deus?')
            return True
        return False

    def get_max_size(self, stdscr: curses.window) -> tuple:
        '''Get current terminal size, set class variables and return tuple(x,y)'''
        self.y, self.x = stdscr.getmaxyx()
        return (self.y, self.x)
      
    #------------------------ NOTIFICATIONS --------------------------#
    def notify(self, message: str, display_time: float = NotificationPriority.NORMAL,
               delimiter: str = '...', external: bool = True) -> None:
        '''Adds a new notification to the notifications queue (thread-safe).'''
        message = str(message)  #(long messages are clipped by the UI when drawn)

        lowered = message.lower()
        urgent = (
            'maximum captcha regens exceeded' in lowered
            or 'manual input' in lowered
            or 'manual intervention' in lowered
            or 'reconnection failed' in lowered
        )
        if urgent and sys.stdout.isatty():
            try:
                curses.beep()
            except Exception:
                try:
                    print(chr(7), end='', flush=True)
                except Exception:
                    pass

        should_send_external = external and any(term in lowered for term in (
            'maximum captcha regens exceeded', 'manual input', 'manual intervention',
            'emerald fish', 'boost purchase', 'buy failed',
            'connection lost', 'reconnection succeeded', 'reconnection failed',
            'connected to discord', 'autofishbot stopped', 'proxy error',
            'captcha detected', 'captcha bypassed', 'incorrect code', 'stopped',
            'watchdog:'
        ))
        if should_send_external and self.notifier:
            try:
                self.notifier.send(message)
            except Exception:
                pass

        important = display_time in (NotificationPriority.HIGH, NotificationPriority.VERY_HIGH)

        with self._notif_lock:
            self.event_logs.append((datetime.now().strftime('%H:%M:%S'), message))
            #Skip exact repeats of what is on screen / already waiting at the end of the queue
            if not important:
                if message == self.current_notification:
                    return None
                if self.notification_queue and self.notification_queue[-1][0] == message:
                    return None

            #Queue full: drop new low-priority messages, make room for important ones
            if len(self.notification_queue) >= MAX_NOTIFICATION_QUEUE:
                if not important:
                    return None
                self.notification_queue = self.notification_queue[:MAX_NOTIFICATION_QUEUE - 1]

            #If high prio, put the notification at the beginning
            if important:
                self.notification_queue.insert(0, (message, display_time))
            else:
                self.notification_queue.append((message, display_time))

    def _err_notification(self, e: str) -> None:
        '''Schedules a notification containing an error message, deduplicating queue entries.'''
        err_message = f'[E] {self.name} error: {e}'
        with self._notif_lock:
            for notification, _ in self.notification_queue:
                if err_message == notification:
                    return None

        debugger.log(e, f'{self.name} - run')
        self.notify(err_message, NotificationPriority.NORMAL)
        return None

    def notifications_thread(self) -> None:
        '''Controls the notification queue and manages its display time.'''
        while True:
            item = None
            backlog = 0
            with self._notif_lock:
                if self.notification_queue:
                    item = self.notification_queue.pop(0)
                    backlog = len(self.notification_queue)
            if item is not None:
                message, display_time = item
                self.current_notification = message
                if backlog >= 5:
                    display_time = min(display_time, 1.0)
                sleep(display_time)
                self.current_notification = ''
            else:
                sleep(self.config.refresh_rate)
    
    #------------------------ FEATURES --------------------------#
    def check_keybinds(self, pressed_key: int, stdscr: curses.window) -> None:
        '''Checks pressed key and trigger its action (if it matches).'''
        if not pressed_key:
            return None
        
        if pressed_key in (ord('w'), ord('W')):
            try:
                self.config.save_automation()
                self.notify('[*] Automation settings saved to config.')
            except Exception as e:
                self.notify(f'[E] Failed to save automation settings: {type(e).__name__}')
            return None

        #Runtime automation toggles (keys 1-7, see AUTOMATION panel)
        feature = TOGGLE_KEYS.get(pressed_key)
        if feature:
            state = self.sch.toggle(feature)
            if state is not None:
                self._config_list = []
                self.notify(f'[*] {feature.replace("_", " ").upper()}: {"ON" if state else "OFF"}')
            return None
        
        for key, action in self.keybinds.list:
            if ord(key) == pressed_key:
                match action:
                    case 'Pause':
                        self.dispatcher.toggle_pause()
                    case 'Quit':
                        self.kill()
                    case 'Sell Inventory':
                        self.sch.schedule(self.sch.commands.sell)
                    case 'Update Inventory':
                        if self.name == 'CompactMenu':
                            self.popup(
                                stdscr=stdscr,
                                command=self.sch.commands.profile,
                                title='Inventory', 
                                items=self.profile, 
                            )
                        else:
                            self.sch.schedule(self.sch.commands.profile)
                    case 'Update Leaderboards':
                        if self.name == 'CompactMenu':
                            self.popup(
                                stdscr=stdscr,
                                command=self.sch.commands.pos,
                                title='Leaderboards', 
                                items=self.profile.leaderboard, 
                            )
                        else:
                            self.sch.schedule(self.sch.commands.pos)
                    case 'Update Cosmetics':
                        self.notify('[!] Update Cosmetics is not implemented yet.', NotificationPriority.LOW)
                    case 'Buy Morefish':
                        self.sch.schedule(self.sch.commands.morefish)
                    case 'Buy Moretreasures':
                        self.sch.schedule(self.sch.commands.moretreasure)
                    case 'Claim Daily':
                        self.sch.schedule(self.sch.commands.daily)
                    case 'Show Quests':
                        self.popup(
                            stdscr=stdscr,
                            command=self.sch.commands.quests,
                            title='Quests', 
                            items=self.profile.quests, 
                        )
                    case 'Show Current Inv':
                        self.popup(
                            stdscr=stdscr,
                            command=self.sch.commands.profile,
                            title='Current Inventory',
                            items=self.profile.inventory,
                        )
                    case 'Show Charms':
                        self.popup(
                            stdscr=stdscr, 
                            command=self.sch.commands.charms,
                            title='Charms', 
                            items=self.profile.charms, 
                        )
                    case 'Show Buffs':
                        self.popup(
                            stdscr=stdscr,
                            command=self.sch.commands.buffs,
                            title='Buffs', 
                            items=self.profile.buffs, 
                        )
                    case 'Show Exotic Fishes':
                        self.popup(
                            stdscr=stdscr,
                            command=self.sch.commands.profile,
                            title='Exotic Fish', 
                            items=self.profile.exotic_fish, 
                        )
                    case _:
                        return None
                    
    def popup(self, stdscr: curses.window, command: Commands, title: str = 'Popup', items: object = []) -> None:    
        '''Show a popup window in the middle of the screen. Used to display quick info.
        The `element: object` argument expects to be a Profile variable containing:
        - `list -> list[tuple]` property
        - `last_update: float` attribute'''

        pressed_key = None
        processed_list = []
        last_update = items.last_update
        choice = '[press \'q\' to return...]'
        update = 'press \'u\' to update...'

        if items.list == []:
            #Auto schedule item if list is empty
            self.sch.schedule(command)

        def construct_lists(_tmp = []) -> list:
            '''Constructs the tupled object list to a string format and returns a tuple
            with (list, objectlast_update)'''
            match title:
                case 'Quests':
                    _tmp = [f'{a} ({b})' for _, a, b in items.list]
                case 'Charms':
                    _tmp = [f'{b} {a}' for a, b in items.list]
                case 'Buffs' | 'Inventory':
                    _tmp = [f'{a}: {b}' for a, b in items.list]
                case 'Current Inventory' | 'Exotic Fish':    
                    _tmp = [f'{a} x {b}' for a, b in items.list]
                case 'Leaderboards':
                    _tmp = [f'{a}: #{b}' for a, b in items.list]
                case _:
                    _tmp = []

            return (_tmp, items.last_update)

        while True:
            try:
                #Check keybinds
                if pressed_key == ord('q'):
                    break
                elif pressed_key == ord('u'):
                    self.sch.schedule(command)
                    
                stdscr.erase()
                
                #Update lists
                if processed_list == [] or (items.last_update != last_update):
                    processed_list, last_update = construct_lists()
                    update_msg = f'[Last update: {convert_time(last_update)} | {update}] '
                
                #Calculations
                #Todo: rework these (prob something realted to odd list length)
                self.get_max_size(stdscr)
                box_size_x = (max(len(line) for line in processed_list)) + 8 if processed_list != [] else 32
                box_size_y = len(processed_list) + 4 if processed_list != [] else 12
                x = (self.x // 2) - (box_size_x // 2)
                y = (self.y // 2) - (box_size_y // 2)
                middle_x = self.x // 2
                middle_y = self.y // 2
                rel_x = (self.x - x) #+ (1 if self.x % 2 == 0 else 2)
                rel_y = (self.y - y) + (0 if self.y % 2 == 0 else -1)

                if self.check_dimentions():
                    for k, data in enumerate(self.minimum_info):
                        stdscr.addstr(middle_y + k - 3, middle_x - ceil(len(data) / 2), data)
                else:
                    #Box
                    stdscr.hline(y - 1, x - 1, curses.ACS_HLINE, box_size_x + 2)
                    stdscr.hline(rel_y, x, curses.ACS_HLINE, box_size_x + 1)
                    stdscr.vline(y, x - 1, curses.ACS_VLINE, box_size_y)
                    stdscr.vline(y, rel_x + 1, curses.ACS_VLINE, box_size_y)
                    
                    #Notification bar
                    stdscr.hline(1, 0, curses.ACS_HLINE, self.x) #Top
                    stdscr.addstr(0, 0, self.notification)
                    
                    #Object list
                    stdscr.addstr(y - 1, middle_x - (len(title) // 2), title)
                    for k, data in enumerate(processed_list):
                        popup_x = middle_x - (len(data) // 2)
                        stdscr.addstr(y + k + 1, popup_x, data)

                    #Exit message
                    stdscr.addstr(rel_y, middle_x - (len(choice) // 2) + 1, choice)

                    #Last update message
                    stdscr.addstr(self.y - 3, middle_x - (len(update_msg) // 2) + 1, update_msg)
                
                stdscr.refresh()
                sleep(self.config.refresh_rate)
                pressed_key = stdscr.getch()
            
            except KeyboardInterrupt:
                break
            except curses.error as e:
                self._err_notification(e)
                sleep(self.config.refresh_rate)
            
        stdscr.erase()
        return None

    #------------------------ MAIN --------------------------#
    def __run__(self, stdscr: curses.window, threads: list[Thread]) -> None:
        '''[Boilerplate function]: Starts the menu main loop, display info on screen 
        and manages user input.

        Quick notes:
        - stdscr.addstr(y, x, string)
        - stdscr.vline/hline(starting_y, starting_x, line_character, line_length)
        (for line_character use curses constants)
        Full documentation: https://docs.python.org/3/library/curses.html
        '''
        
        curses.curs_set(0)
        curses.noecho()
        stdscr.nodelay(True)
        
        self.is_alive = True

        while self.is_alive:
            #Checks for essential threads activitys
            for thread in threads:
                if not thread.is_alive():
                    self.kill()
                    curses.endwin()
                    exit(f'[E] "{thread.name}" thread exited.')
            try:
                stdscr.refresh()
                stdscr.erase()
                
                #Calculations
                self.get_max_size(stdscr)
                middle_x, middle_y = (self.x // 2),  (self.y // 2)
                #...

                if self.check_dimentions():
                    for k, data in enumerate(self.minimum_info):
                        stdscr.addstr(middle_y + k - 3, middle_x - ceil(len(data) / 2), data)
                else:
                    #------------------------ LINES --------------------------#
                    #...
                    
                    #------------------------ ASCII --------------------------#
                    #...
                    
                    #------------------------ TEXTS --------------------------#
                    #...
                    pass
                
                sleep(self.config.refresh_rate)
                stdscr.refresh()
                self.check_keybinds(stdscr.getch(), stdscr)

            except KeyboardInterrupt:
                self.kill()
            except Exception as e:
                self._err_notification(e)
                sleep(self.config.refresh_rate)
        

    
#------------------------ MODERN UI --------------------------#
#Color pair ids
P_ACC, P_OK, P_WARN, P_ERR, P_MAG, P_BAR, P_BOK, P_BWARN, P_BERR, P_BINFO = range(1, 11)


def _fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f'{h}h{m:02d}m' if h else f'{m}m{sec:02d}s'


class ModernUI:
    '''Responsive curses renderer shared by MainMenu and CompactMenu.
    Layouts: wide (3 columns) / medium (2 columns) / narrow (stacked, phone portrait).'''

    SPIN_U = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    SPIN_A = '|/-\\'

    def __init__(self, menu: 'BaseMenu') -> None:
        self.m = menu
        self.scr = None
        self.H = self.W = 0
        u = menu._unicode
        self.g = dict(
            tl='╭' if u else '+', tr='╮' if u else '+', bl='╰' if u else '+', br='╯' if u else '+',
            hz='─' if u else '-', vt='│' if u else '|', dot='●' if u else '*', warn='▲' if u else '!',
            err='✖' if u else 'x', star='★' if u else '*', bullet='•' if u else '-', sep=' · ' if u else ' | ',
            logo='◆' if u else '#', more='…' if u else '...')
        self.spin = self.SPIN_U if u else self.SPIN_A

    #------------------------ primitives --------------------------#
    def attr(self, pair: int = 0, bold: bool = False, dim: bool = False, rev: bool = False) -> int:
        a = curses.color_pair(pair) if (self.m._colors and pair) else 0
        if bold: a |= curses.A_BOLD
        if dim: a |= curses.A_DIM
        if rev: a |= curses.A_REVERSE
        return a

    def put(self, y: int, x: int, text: str, attr: int = 0, maxw: int | None = None) -> int:
        '''Clipped addstr, returns the number of columns written.'''
        if y < 0 or y >= self.H or x >= self.W or not text:
            return 0
        if x < 0:
            text, x = text[-x:], 0
        limit = self.W - x if maxw is None else min(maxw, self.W - x)
        if limit <= 0:
            return 0
        text = str(text)[:limit]
        self.scr.addstr(y, x, text, attr)
        return len(text)

    def clip(self, text: str, w: int) -> str:
        text = str(text)
        if len(text) <= w:
            return text
        return text[:max(0, w - 1)] + self.g['more'] if w > 1 else text[:w]

    def box(self, y: int, x: int, h: int, w: int, title: str = '', right: str = '', pair: int = P_ACC) -> None:
        if h < 3 or w < 6:
            return
        g, a = self.g, self.attr(pair, dim=True)
        self.put(y, x, g['tl'] + g['hz'] * (w - 2) + g['tr'], a)
        for i in range(1, h - 1):
            self.put(y + i, x, g['vt'], a)
            self.put(y + i, x + w - 1, g['vt'], a)
        self.put(y + h - 1, x, g['bl'] + g['hz'] * (w - 2) + g['br'], a)
        if title:
            self.put(y, x + 2, f' {self.clip(title, w - 8)} ', self.attr(pair, bold=True))
        if right:
            r = f' {self.clip(right, w - 8)} '
            self.put(y, x + w - 2 - len(r), r, self.attr(0, dim=True))

    def kv(self, y: int, x: int, w: int, key: str, val: str, vattr: int = 0) -> None:
        val = self.clip(val, max(1, w - 3))
        kw = max(0, w - len(val) - 1)
        self.put(y, x, self.clip(key, kw), self.attr(0, dim=True))
        self.put(y, x + w - len(val), val, vattr)

    #------------------------ data helpers --------------------------#
    def state(self) -> tuple[str, int, int]:
        '''Returns (text, header_badge_pair, text_pair).'''
        m = self.m
        try:
            d, sch = m.dispatcher, m.sch
            cap = getattr(d, 'captcha', None)
            if cap is not None and getattr(cap, 'detected', False):
                return ('CAPTCHA', P_BERR, P_ERR)
            if d.paused:
                return ('PAUSED', P_BWARN, P_WARN)
            if sch.status == SchStatus.BREAK:
                return ('BREAK', P_BINFO, P_MAG)
            if sch.status == SchStatus.BUSY:
                return ('WAITING', P_BWARN, P_WARN)
        except Exception:
            pass
        return ('FISHING', P_BOK, P_OK)

    def rate(self) -> float:
        mins = (time() - self.m.started) / 60
        return (self.m.rcv_fish / mins) if mins > 0.05 else 0.0

    @staticmethod
    def _money(value: float | None) -> str:
        return '-' if value is None else f'${value:,.2f}'

    @classmethod
    def _money_rate(cls, value: float | None) -> str:
        return '-' if value is None else f'{cls._money(value)}/h'

    def stats_rows(self) -> list[tuple]:
        m = self.m
        st, _, sp = self.state()
        try:
            cd = m.dispatcher.cooldown.last
            cd = f'{cd:.2f}s' if cd else '-'
        except Exception:
            cd = '-'
        try:
            sch = m.sch.status.name.title()
            if m.sch.status == SchStatus.BREAK:
                sch = f'Break {m.sch._break_remaining:.0f}s'
            sch = f'{sch} ({len(m.sch.queue)})'
        except Exception:
            sch = '-'
        return [
            ('State', st, self.attr(sp, bold=True)),
            ('Streak', str(m.rcv_streak), self.attr(0, bold=True)),
            ('Fish msgs', str(m.rcv_fish), self.attr(P_ACC, bold=True)),
            ('Rate', f'{self.rate():.1f}/min', 0),
            ('Catch/hr', f'{m.rcv_fish / max((time() - m.started) / 3600, 1/3600):.1f}', self.attr(P_ACC, bold=True)),
            ('Balance/hr', self._money_rate(m.session_stats.balance_per_hour()), 0),
            ('Auto sell', self._money(m.session_stats.snapshot()[0]), 0),
            ('Bypasses', str(m.rcv_bypasses), self.attr(P_MAG if m.rcv_bypasses else 0)),
            ('Cooldown', cd, 0),
            ('Scheduler', sch, 0),
            ('Uptime', _fmt_time(time() - m.started), 0),
        ]

    #------------------------ bars --------------------------#
    def header(self, y: int) -> None:
        W, m = self.W, self.m
        bar = self.attr(P_BAR, bold=True)
        self.put(y, 0, ' ' * W, bar)
        self.put(y, 0, f' {self.g["logo"]} AUTOFISHBOT ', bar)
        st, badge, _ = self.state()
        spin = self.spin[int(time() * 8) % len(self.spin)] if st == 'FISHING' else self.g['dot']
        self.put(y, 16, f' {spin} {st} ', self.attr(badge, bold=True))
        right = f'{_fmt_time(time() - m.started)}'
        if W >= 60:
            right += f'  {datetime.now().strftime("%H:%M:%S")}'
        if W - len(right) - 1 > 16 + len(st) + 6:
            self.put(y, W - len(right) - 1, right, bar)

    def notif(self, y: int) -> None:
        m, g = self.m, self.g
        msg = (m.current_notification or '').strip()
        q = len(m.notification_queue)
        if not msg:
            self.put(y, 1, 'Nothing new.', self.attr(0, dim=True))
            return
        pair, icon = P_ACC, g['dot']
        if msg.startswith('[E]'):
            pair, icon, msg = P_ERR, g['err'], msg[3:]
        elif msg.startswith('[!]'):
            pair, icon, msg = P_WARN, g['warn'], msg[3:]
        elif msg.startswith('[*]'):
            msg = msg[3:]
        tail = f' +{q}' if q else ''
        self.put(y, 1, icon, self.attr(pair, bold=True))
        self.put(y, 3, self.clip(msg.strip(), self.W - 5 - len(tail)), self.attr(pair if pair != P_ACC else 0))
        if tail:
            self.put(y, self.W - len(tail) - 1, tail, self.attr(0, dim=True))

    def footer(self, y: int) -> None:
        x = 1
        try:
            binds = self.m.keybinds.list
        except Exception:
            return
        for key, action in binds:
            seg_w = len(key) + len(action) + 3
            if x + seg_w >= self.W:
                break
            self.put(y, x, key, self.attr(P_ACC, bold=True))
            self.put(y, x + len(key) + 1, action, self.attr(0, dim=True))
            x += seg_w + 1
        if x + 16 < self.W:
            self.put(y, x, 'W Save toggles', self.attr(P_ACC, bold=True))

    #------------------------ panels --------------------------#
    def p_stats(self, y: int, x: int, h: int, w: int) -> None:
        self.box(y, x, h, w, 'STATS')
        for i, (k, v, a) in enumerate(self.stats_rows()[:h - 2]):
            self.kv(y + 1 + i, x + 2, w - 4, k, v, a)

    def p_config(self, y: int, x: int, h: int, w: int) -> None:
        self.box(y, x, h, w, 'AUTOMATION', pair=P_ACC)
        for i, (k, v) in enumerate(self.m.config_list[:h - 2]):
            on = str(v).upper() == 'ON'
            self.kv(y + 1 + i, x + 2, w - 4, k.title(), f'{self.g["dot"]} {v}' if on else f'{self.g["dot"]} {v}',
                    self.attr(P_OK if on else P_ERR, bold=on, dim=not on))

    def p_catch(self, y: int, x: int, h: int, w: int) -> None:
        m, g = self.m, self.g
        self.box(y, x, h, w, 'CATCH', f'#{m.rcv_streak}' if m.rcv_streak else '')
        lines = [sub(r'\s{2,}', ' ', sub(r':\w+:', '', l)).strip() for l in m.items if l and l.strip()]
        inner_h, inner_w = h - 2, w - 4
        if not lines:
            sp = self.spin[int(time() * 8) % len(self.spin)]
            self.put(y + 1, x + 2, f'{sp} Waiting for catch{g["more"]}', self.attr(0, dim=True))
            if time() - m.started > 30 and m.rcv_streak == 0 and inner_h >= 3:
                self.put(y + 2, x + 2, self.clip(f'{g["warn"]} No fish message detected yet.', inner_w), self.attr(P_WARN))
                if inner_h >= 4:
                    self.put(y + 3, x + 2, self.clip('See dumps/unknown_events.jsonl', inner_w), self.attr(0, dim=True))
            return
        shown = lines[:inner_h] if len(lines) <= inner_h else lines[:inner_h - 1]
        for i, line in enumerate(shown):
            if 'LEVEL UP' in line:
                txt = f'{g["star"]} {line.strip("<> ")}'
                self.put(y + 1 + i, x + 2, self.clip(txt, inner_w), self.attr(P_MAG, bold=True))
            elif sub(r'^\W*\d+\s*x', '', line) != line:
                self.put(y + 1 + i, x + 2, self.clip(f'{g["bullet"]} {line}', inner_w), self.attr(P_OK))
            else:
                self.put(y + 1 + i, x + 2, self.clip(f'{g["bullet"]} {line}', inner_w), 0)
        if len(lines) > len(shown):
            self.put(y + inner_h, x + 2, f'{g["more"]} +{len(lines) - len(shown)} more', self.attr(0, dim=True))

    def p_recent(self, y: int, x: int, h: int, w: int) -> None:
        self.box(y, x, h, w, 'RECENT', f'{len(self.m.log)} logged' if self.m.log else '')
        rows = list(reversed(self.m.log))[:h - 2]
        for i, (ts, text) in enumerate(rows):
            self.put(y + 1 + i, x + 2, ts, self.attr(0, dim=True))
            self.put(y + 1 + i, x + 2 + len(ts) + 2, self.clip(text, w - 6 - len(ts)), 0)
        if not rows:
            self.put(y + 1, x + 2, 'No catches yet.', self.attr(0, dim=True))

    def p_list(self, y: int, x: int, h: int, w: int, title: str, rows: list, prefix: str = '') -> None:
        self.box(y, x, h, w, title)
        if not rows:
            hint = 'No data. Press I to load.' if title == 'INVENTORY' else 'No data. Press L to load.'
            self.put(y + 1, x + 2, self.clip(hint, w - 4), self.attr(0, dim=True))
            return
        for i, row in enumerate(rows[:h - 2]):
            a, b = str(row[0]), str(row[1])
            self.kv(y + 1 + i, x + 2, w - 4, a, f'{prefix}{b}', self.attr(0, bold=True))

    def safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            debugger.log(e, f'ModernUI - {getattr(fn, "__name__", "panel")}')

    #------------------------ layouts --------------------------#
    def draw(self, scr, compact: bool) -> None:
        self.scr = scr
        self.H, self.W = scr.getmaxyx()
        H, W, m = self.H, self.W, self.m

        self.safe(self.header, 0)
        self.safe(self.notif, 1)
        self.safe(self.footer, H - 1)
        by, bh = 2, H - 3  #body area

        if bh < 3:
            return

        if W >= 100 and H >= 26 and not compact:
            lw, rw = 30, 32
            cw = W - lw - rw
            sh = min(13, bh)
            self.safe(self.p_stats, by, 0, sh, lw)
            if bh - sh >= 5:
                self.safe(self.p_config, by + sh, 0, bh - sh, lw)
            ch = max(8, bh * 55 // 100)
            self.safe(self.p_catch, by, lw, ch, cw)
            if bh - ch >= 5:
                self.safe(self.p_recent, by + ch, lw, bh - ch, cw)
            ih = max(5, bh * 55 // 100)
            self.safe(self.p_list, by, lw + cw, ih, rw, 'INVENTORY', self._inv())
            if bh - ih >= 5:
                self.safe(self.p_list, by + ih, lw + cw, bh - ih, rw, 'LEADERBOARDS', self._lea(), '#')

        elif W >= 60 and H >= 14:
            rw = min(36, max(26, W * 36 // 100))
            cw = W - rw
            sh = min(13, bh)
            self.safe(self.p_stats, by, cw, sh, rw)
            if bh - sh >= 5:
                self.safe(self.p_config, by + sh, cw, bh - sh, rw)
            ch = bh if bh < 18 else bh * 60 // 100
            self.safe(self.p_catch, by, 0, ch, cw)
            if bh - ch >= 5:
                self.safe(self.p_recent, by + ch, 0, bh - ch, cw)

        else:  #narrow / phone portrait
            used = 0
            rows = self.stats_rows()
            if bh >= 13:
                self.box(by, 0, 5, W, 'SESSION')
                st, _, sp = self.state()
                l1 = f'{st}  {m.rcv_streak} streak  {m.rcv_fish} fish  {self.rate()*60:.1f}/h'
                l2 = f'Balance/h {rows[5][1]}  Auto sell {rows[6][1]}'
                l3 = f'cd {rows[8][1]}  bypass {m.rcv_bypasses}'
                self.safe(self.put, by + 1, 2, self.clip(l1, W - 4), self.attr(sp, bold=True))
                self.safe(self.put, by + 2, 2, self.clip(l2, W - 4), self.attr(0, dim=True))
                self.safe(self.put, by + 3, 2, self.clip(l3, W - 4), self.attr(0, dim=True))
                used = 5
            elif bh >= 12:
                self.box(by, 0, 4, W, 'STATS')
                st, _, sp = self.state()
                l1 = f'{st}  {m.rcv_streak} streak  {m.rcv_fish} fish'
                l2 = f'{self.rate():.1f}/min  cd {rows[8][1]}  bypass {m.rcv_bypasses}'
                self.safe(self.put, by + 1, 2, self.clip(l1, W - 4), self.attr(sp, bold=True))
                self.safe(self.put, by + 2, 2, self.clip(l2, W - 4), self.attr(0, dim=True))
                used = 4
            else:
                st, _, sp = self.state()
                line = f'{st} {m.rcv_streak} streak {m.rcv_fish} fish {self.rate():.1f}/m'
                self.safe(self.put, by, 1, self.clip(line, W - 2), self.attr(sp, bold=True))
                used = 1
            self.safe(self.p_catch, by + used, 0, bh - used, W)

    def _inv(self) -> list:
        try:
            return list(self.m.profile.list)
        except Exception:
            return []

    def _lea(self) -> list:
        try:
            return list(self.m.profile.leaderboard.list)
        except Exception:
            return []


def curses_init(self: 'BaseMenu', stdscr: curses.window) -> None:
    '''Cursor/keyboard/colors setup (shared by the single and multi account UIs).'''
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.noecho()
    stdscr.nodelay(True)

    try:
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for pid, fg, back in [
                (P_ACC, curses.COLOR_CYAN, bg), (P_OK, curses.COLOR_GREEN, bg),
                (P_WARN, curses.COLOR_YELLOW, bg), (P_ERR, curses.COLOR_RED, bg),
                (P_MAG, curses.COLOR_MAGENTA, bg),
                (P_BAR, curses.COLOR_WHITE, curses.COLOR_BLUE),
                (P_BOK, curses.COLOR_BLACK, curses.COLOR_GREEN),
                (P_BWARN, curses.COLOR_BLACK, curses.COLOR_YELLOW),
                (P_BERR, curses.COLOR_WHITE, curses.COLOR_RED),
                (P_BINFO, curses.COLOR_WHITE, curses.COLOR_MAGENTA),
            ]:
                curses.init_pair(pid, fg, back)
            self._colors = True
    except curses.error:
        self._colors = False


def _menu_loop(self: 'BaseMenu', stdscr: curses.window, threads: list[Thread], compact: bool) -> None:
    '''Shared main loop for MainMenu and CompactMenu.'''
    curses_init(self, stdscr)

    ui = ModernUI(self)
    self.is_alive = True

    while self.is_alive:
        #Checks for essential threads activitys
        for thread in threads:
            if not thread.is_alive():
                self.kill()
                curses.endwin()
                exit(f'[E] "{thread.name}" thread exited.')
        try:
            self.get_max_size(stdscr)
            stdscr.erase()

            if self.check_dimentions():
                for k, data in enumerate(self.minimum_info):
                    stdscr.addstr(max(0, (self.y // 2) + k - 1), max(0, (self.x // 2) - ceil(len(data) / 2)), data[:max(1, self.x - 1)])
            else:
                ui.draw(stdscr, compact)

            stdscr.refresh()
            sleep(min(self.config.refresh_rate, 0.25))
            self.check_keybinds(stdscr.getch(), stdscr)

        except KeyboardInterrupt:
            self.kill()
        except Exception as e:
            self._err_notification(e)
            sleep(self.config.refresh_rate)


@dataclass(slots=True)
class MainMenu(BaseMenu):
    minimum: tuple = (28, 8)

    def __run__(self, stdscr: curses.window, threads: list[Thread]) -> None:
        _menu_loop(self, stdscr, threads, compact=False)


@dataclass(slots=True)
class CompactMenu(BaseMenu):
    minimum: tuple = (28, 8)

    def __run__(self, stdscr: curses.window, threads: list[Thread]) -> None:
        _menu_loop(self, stdscr, threads, compact=True)

# --------- INIT ---------#
if __name__ == "__main__":
    pass