#------------------------ IMPORTS --------------------------#
from __future__ import annotations

from . import *
from .cooldown import CooldownManager
from enum import Enum, auto
from threading import Event, Lock
from time import sleep, time
from random import random, uniform, randint


ONCE = 24 * 60 * 60
DEFAULT = 5 * 60


class SchStatus(Enum):
    READY = auto()
    BUSY = auto()
    WAITING = auto()
    BREAK = auto()


@dataclass(slots=True)
class CommandType:
    cmd: str
    default_cd: float = DEFAULT
    field_name: str | None = field(default=None, repr=False)
    value: str | None = field(default=None, repr=False)
    event: dict | None = None
    last_usage: float = 0
    block_requests: bool = False

    def __post_init__(self) -> None:
        if self.default_cd != ONCE:
            #Adds some randomization for each base cooldown
            if self.cmd != 'buy':
                self.default_cd += uniform(-120, 240)
            else:
                self.default_cd += uniform(-120, 360)

        if self.field_name and self.value:
            self.event = {
                'type': 3,
                'name': self.field_name,
                'value': self.value
            }

    def __repr__(self) -> str:
        return self.cmd

    @property
    def data(self) -> tuple[str, dict | None]:
        self.last_usage = time()
        return (self.cmd, self.event)


class Commands:
    def __init__(self, config: ConfigManager) -> None:
        #Builds value strings
        bait_cd, bait_value = self._make_bait(config)
        mf_value, mt_value, boosts_cd = self._make_boosts(config.boosts_length)

        #Commands lock (if automated, manual requests are restricted)
        daily_lock = config.auto_daily
        mf_lock, mt_lock = config.more_fish, config.more_treasures

        #Info
        self.profile: CommandType = CommandType('profile', 10 * 60)
        self.pos: CommandType = CommandType('pos')
        self.quests: CommandType = CommandType('quests')
        self.charms: CommandType = CommandType('charms')
        self.buffs: CommandType = CommandType('buffs')
        self.show_rods: CommandType = CommandType('rod')
        self.show_biomes: CommandType = CommandType('biome')
        self.daily: CommandType = CommandType('daily', ONCE, block_requests=daily_lock)

        #Buy and sell
        self.sell: CommandType = CommandType('sell', 8 * 60, 'amount', 'all')
        self.bait: CommandType = CommandType('buy', bait_cd, 'item', bait_value)
        self.worker: CommandType = CommandType('buy', ONCE, 'item', 'auto30m')
        self.morefish: CommandType = CommandType('buy', boosts_cd, 'item', mf_value, block_requests=mf_lock)
        self.moretreasure: CommandType = CommandType('buy', boosts_cd, 'item', mt_value, block_requests=mt_lock)

        #Select
        self.select_pet: CommandType = CommandType('pet', DEFAULT, 'selection', config.pet or None)
        self.select_biome: CommandType = CommandType('biome', DEFAULT, 'selection', config.biome or None)
        self.select_bait: CommandType = CommandType('bait', DEFAULT, 'selection', config.bait or None)
        self.select_rod: CommandType = CommandType('rod', DEFAULT, 'selection', None)

    @property
    def moretreausre(self) -> CommandType:
        '''Backwards-compatible alias for moretreasure.'''
        return self.moretreasure

    def __iter__(self):
        for value in self.__dict__.values():
            if isinstance(value, CommandType):
                yield value

    def _make_boosts(self, length: int) -> tuple[str, str, int]:
        #Fallback cooldown only (boost ended message triggers the real rebuy), must be > boost length
        return (f'fish{length}m', f'treasure{length}m', length * 60 + 240)

    def _make_bait(self, config: ConfigManager) -> tuple[float, str | None]:
        if config.bait:
            bait_cd = randint(15, 30) * 60
            bait_amount = max(10, round(((bait_cd / config.user_cooldown) - 10) * 0.30, -1))
            bait_value = f'{config.bait} {int(bait_amount)}'
        else:
            bait_cd, bait_value = ONCE, None
        return (bait_cd, bait_value)


@dataclass(slots=True)
class Scheduler:
    session: DiscordWrapper
    config: ConfigManager
    menu: BaseMenu
    captcha: Captcha
    dsp: object = field(init=False) #Dispatcher
    stop_event: Event = field(default_factory=Event, repr=False)

    cooldown: CooldownManager = field(init=False)
    queue: list = field(default_factory=list)
    pending: list = field(default_factory=list) #Queue ops requested by other threads (applied by scheduler thread)
    buy_failures: dict = field(default_factory=dict)
    buy_backoff_until: dict = field(default_factory=dict)
    pending_boost_command: CommandType | None = None
    commands: Commands = field(init=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    last_break: float = field(default_factory=time)
    current_break_interval: float = 0.0

    #Scheduled breaks disabled (24/7 continuous mode)
    _break_remaining: float = 0.0

    status: SchStatus = SchStatus.BUSY
    global_cooldown: float = 10.0 #Global commands cooldown (in seconds)

    def __post_init__(self) -> None:
        self.commands = Commands(self.config)
        self.cooldown = CooldownManager(self.global_cooldown, randint(2, 4))
        self.setup()

    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'

    @property
    def waiting_time(self) -> float:
        value = self.cooldown.custom(
            mu=randint(1, 4) + random(),
            sigma=random()
        )
        return value if value > 0 else 1 + random()

    def new_interval(self) -> None:
        self.current_break_interval = 0.0

    def check_task(self, cmd: CommandType, manual: bool) -> bool:
        now = time()
        with self._lock:
            backoff_until = self.buy_backoff_until.get(id(cmd), 0)
        cond1 = cmd.last_usage == 0
        cond2 = (now - cmd.last_usage) >= cmd.default_cd
        cond3 = bool(manual)
        cond4 = not self.captcha.detected
        cond5 = not self.dsp.paused
        cond6 = now >= backoff_until
        return (cond1 or cond2 or cond3) and cond4 and cond5 and cond6

    def interrupt_break(self) -> None:
        '''Kept for compatibility; scheduled breaks are disabled (24/7 mode).'''
        if self.status == SchStatus.BREAK:
            self.status = SchStatus.WAITING

    def break_check(self) -> None:
        '''Scheduled breaks are disabled so the bot runs continuously 24/7.'''
        return None

    def purge_items(self, arr: list, idx: list) -> list:
        '''Removes elements from a list by a list of indexes.'''
        if not idx:
            return arr
        idx_set = set(idx)
        return [element for index, element in enumerate(arr) if index not in idx_set]

    def run(self, dispatcher: object) -> None:
        self.dsp = dispatcher
        while not self.stop_event.is_set():
            removable = []
            self._apply_pending()
            if self.stop_event.is_set():
                break
            self.status = SchStatus.READY

            #Waits while paused
            while self.dsp.paused and not self.stop_event.is_set():
                self.stop_event.wait(0.5)
            if self.stop_event.is_set():
                break

            buy_cmds = (self.commands.morefish, self.commands.moretreasure, self.commands.bait)
            for index, task in enumerate(list(self.queue)):
                if self.stop_event.is_set():
                    break
                command, persist, manual = task
                if self.check_task(command, manual):
                    self.status = SchStatus.BUSY

                    if self.stop_event.wait(self.waiting_time):
                        break

                    cmd, data = command.data
                    if command in buy_cmds:
                        with self._lock:
                            self.pending_boost_command = command
                    if command is self.commands.sell and persist:
                        self.menu.session_stats.mark_auto_sell()
                    request_result = self.session.request(command=cmd, parameters=data)
                    if request_result is False or request_result is None:
                        if command is self.commands.sell and persist:
                            self.menu.session_stats.cancel_auto_sell()
                        if command in buy_cmds:
                            self.register_buy_failure(command, 'request rejected or timed out')

                    if not persist:
                        removable.append(index)

            if self.status == SchStatus.BUSY:
                self.queue = self.purge_items(self.queue, removable)
                if self.stop_event.wait(self.waiting_time):
                    break
                self.status = SchStatus.WAITING
                self.stop_event.wait(max(0.5, self.cooldown.new()))
                continue
            else:
                self.stop_event.wait(2.0)

    def make_delay(self, secs: float, command: CommandType) -> None:
        '''Emulates last_usage so command becomes ready in `secs` seconds.'''
        command.last_usage = (time() - command.default_cd) + secs
        return None

    def add(self, command: CommandType, persist: bool = False, manual: bool = True, pre_delay: float | None = None) -> None:
        '''Appends the requested command to the queue.'''
        if pre_delay:
            self.make_delay(pre_delay, command)
        self.queue.append((command, persist, manual))

    def register_buy_failure(self, command: CommandType | None, reason: str) -> None:
        buy_cmds = (self.commands.morefish, self.commands.moretreasure, self.commands.bait)
        with self._lock:
            if command not in buy_cmds:
                command = self.pending_boost_command
            if command is None:
                return
            key = id(command)
            failures = self.buy_failures.get(key, 0) + 1
            self.buy_failures[key] = failures
            delay = min(60 * 60, 30 * (2 ** min(failures - 1, 7)))
            self.buy_backoff_until[key] = time() + delay
            command.last_usage = time() - command.default_cd + delay
        kind = 'More Fish' if command is self.commands.morefish else (
            'More Treasures' if command is self.commands.moretreasure else 'Bait'
        )
        self.menu.notify(f'[!] {kind} buy failed: {reason}. Retry in {int(delay)}s.')

    def register_buy_success(self, command: CommandType | None = None) -> None:
        with self._lock:
            command = command or self.pending_boost_command
            if command is None:
                return
            self.buy_failures.pop(id(command), None)
            self.buy_backoff_until.pop(id(command), None)
            if command is self.pending_boost_command:
                self.pending_boost_command = None

    def inspect_purchase_message(self, text: str) -> bool:
        lower = (text or '').lower()
        if not lower:
            return False
        with self._lock:
            pending_cmd = self.pending_boost_command
        if pending_cmd is None:
            return False

        insufficient = (
            ('emerald' in lower and any(term in lower for term in (
                'not enough', 'insufficient', 'need', 'lack', 'cannot afford', "can't afford"
            )))
            or any(term in lower for term in (
                'insufficient funds', 'not enough currency', 'not enough coins',
                'insufficient coins', 'not enough money', 'insufficient balance'
            ))
        )
        generic_failure = any(term in lower for term in (
            'purchase failed', 'could not buy', 'unable to buy', 'buy failed'
        ))
        if insufficient or generic_failure:
            reason = ('insufficient Emerald Fish' if 'emerald' in lower else 'insufficient funds') if insufficient else 'purchase rejected by game'
            self.register_buy_failure(pending_cmd, reason)
            return True
        success = any(term in lower for term in (
            'boost activated', 'successfully purchased', 'successfully bought',
            'purchase successful', 'you purchased', 'you bought'
        ))
        if success:
            self.register_buy_success(pending_cmd)
            return True
        return False

    def boost_ended(self, kind: str) -> bool:
        '''Called by the Receiver when the game says a boost ended.'''
        if kind == 'fish' and self.config.more_fish:
            cmd = self.commands.morefish
        elif kind == 'treasure' and self.config.more_treasures:
            cmd = self.commands.moretreasure
        else:
            return False

        self.register_buy_success(cmd)

        if not any(task[0] is cmd for task in list(self.queue)):
            return False

        self.make_delay(uniform(2, 8), cmd)
        self.menu.notify(f'[*] {kind.capitalize()} boost ended, rebuying...')
        return True

    def _apply_pending(self) -> None:
        '''Applies queue changes requested by other threads (runs inside the scheduler thread).'''
        with self._lock:
            ops = list(self.pending)
            self.pending.clear()
        for op, cmd, persist, manual, delay in ops:
            if op == 'remove':
                self.queue = [t for t in self.queue if t[0] is not cmd]
            elif op == 'add':
                self.queue = [t for t in self.queue if t[0] is not cmd]
                self.add(cmd, persist, manual, delay)
            elif op == 'schedule':
                self.add(cmd, persist=False, manual=True)

    def toggle(self, feature: str) -> bool | None:
        '''Turns an automation ON/OFF at runtime. Returns the new state, or None if not possible.'''
        c = self.commands
        table = {
            'more_fish': (c.morefish, True, True),
            'more_treasures': (c.moretreasure, True, True),
            'auto_daily': (c.daily, False, True),
            'auto_sell': (c.sell, True, False),
            'auto_update_inventory': (c.profile, True, False),
            'auto_buy_baits': (c.bait, True, False),
        }
        if feature == 'fish_on_exit':
            self.config.fish_on_exit = not self.config.fish_on_exit
            return self.config.fish_on_exit
        if feature not in table:
            return None
        if feature == 'auto_buy_baits' and not self.config.bait:
            self.menu.notify('[!] Set a bait in the config (COSMETIC > bait) to use auto buy baits.')
            return None

        cmd, persist, lock = table[feature]
        new_state = not getattr(self.config, feature)
        setattr(self.config, feature, new_state)
        if lock:
            cmd.block_requests = new_state

        with self._lock:
            if new_state:
                if cmd.default_cd == ONCE and cmd.last_usage != 0:
                    return new_state
                self.pending.append(('add', cmd, persist, False, uniform(3, 15)))
            else:
                self.pending.append(('remove', cmd, persist, False, None))
        return new_state

    def setup(self) -> None:
        def init_delay(mn: float = 0, mx: float = 5 * 60) -> float:
            return uniform(mn, mx)

        if self.config.auto_daily:
            self.add(self.commands.daily, False, False, init_delay())

        boosts_delay = init_delay(0, 2 * 60)
        if self.config.more_fish:
            self.add(self.commands.morefish, True, False, boosts_delay)
        if self.config.more_treasures:
            self.add(self.commands.moretreasure, True, False, boosts_delay)

        if self.config.auto_buy_baits and self.config.bait:
            self.add(self.commands.bait, True, False, init_delay(5 * 60, 20 * 60))

        if self.config.auto_sell:
            self.add(self.commands.sell, True, False, init_delay(5 * 60, 15 * 60))

        if self.config.auto_update_inventory:
            self.add(self.commands.profile, True, False, init_delay(20, 3 * 60))

    def schedule(self, command: CommandType) -> bool:
        '''Schedules new tasks to the queue, called by user input (thread-safe).'''
        for cmd in self.commands:
            if cmd == command:
                snapshot = list(self.queue)
                with self._lock:
                    pending_cmds = [p[1] for p in self.pending if p[0] == 'schedule']

                not_queued = all(command != k[0] for k in snapshot) and command not in pending_cmds
                allow_profile = (command.cmd == 'profile') and bool(self.config.auto_update_inventory)

                cond1 = not_queued or allow_profile
                cond2 = self.status == SchStatus.READY
                cond3 = not command.block_requests
                cond4 = (command.default_cd != ONCE) or (command.last_usage == 0)
                cond5 = not self.captcha.detected

                if cond1 and cond2 and cond3 and cond4 and cond5:
                    self.menu.notify('[*] Command scheduled.')
                    with self._lock:
                        self.pending.append(('schedule', command, False, True, None))
                    return True
                else:
                    if not cond1:
                        self.menu.notify('[!] Command already scheduled to be executed, please wait.')
                    if not cond2:
                        self.menu.notify('[!] Global commands cooldown, please wait.')
                    if not cond3:
                        self.menu.notify('[!] This command is locked by Scheduler (due to automations).')
                    if not cond4:
                        self.menu.notify('[!] This command is restricted to one usage.')
                    if not cond5:
                        self.menu.notify('[!] Captcha detected, Scheduler is locked.')
                    return False
        return False
