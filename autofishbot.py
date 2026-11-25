#! /usr/bin/env python3
#------------------------ IMPORTS --------------------------#
from __future__ import annotations
from app import *
import sys
from threading import Event, Thread
from time import sleep, time
from app.notifications import NotificationManager
from random import random, uniform
from json import dumps
from os import makedirs

_dump_counts: dict[str, int] = {}


def dump_event(event: dict, name: str = 'unknown_events.jsonl', limit: int = 30) -> None:
    '''Appends raw gateway events to ./dumps/ (max `limit` lines) so the real message format can be inspected.'''
    try:
        #Count lines only once per file (no more disk reads for every unhandled message)
        if _dump_counts.get(name, 0) >= limit:
            return
        makedirs('./dumps', exist_ok=True)
        path = f'./dumps/{name}'
        if name not in _dump_counts:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    _dump_counts[name] = sum(1 for _ in f)
            except FileNotFoundError:
                _dump_counts[name] = 0
            if _dump_counts[name] >= limit:
                return
        with open(path, 'a', encoding='utf-8') as f:
            f.write(dumps(event, ensure_ascii=False) + '\n')
        _dump_counts[name] += 1
    except Exception:
        pass


def event_owner(event: dict) -> str:
    '''Returns the id of the user who triggered the interaction that originated this message (or '').'''
    for key in ('interaction_metadata', 'interaction'):
        try:
            owner = (event.get(key) or {}).get('user', {}).get('id')
        except AttributeError:
            owner = None
        if owner:
            return str(owner)
    return ''


@dataclass(slots=True)
class Receiver:
    #Pointers
    session: DiscordWrapper
    config: ConfigManager
    menu: BaseMenu

    #Objects
    captcha: Captcha = field(init=False)
    profile: Profile = field(default_factory=Profile)
    message: Message = field(default_factory=Message)
    category: MessageCategory = field(default_factory=MessageCategory)
    event: dict | None = None
    sch: object = None #Scheduler pointer (set after instantiation)
    stop_event: Event = field(default_factory=Event, repr=False)

    #Flags
    is_ready: bool = False

    def __post_init__(self) -> None:
        '''Setups captcha.'''
        self.captcha = Captcha(api_key=self.config.ocr_api_key, menu=self.menu)

    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'

    def check_event(self, response: dict | None) -> bool:
        '''Checks if event is targeted to the user (sent by the application to the selected channel),
        also defines self.event.'''
        if not response:
            return False

        try:
            e = response['d']
            e_name = response['t']
            e_channel = e['channel_id']
            e_author = e['author']['id']
        except (KeyError, TypeError):
            return False

        if e_name in TARGET_EVENT_NAMES:
            if e_channel == self.config.channel_id:
                if e_author == APPLICATION_ID:
                    #Same channel shared by several accounts / players: only accept messages
                    #that belong to THIS account (when Discord tells us who they belong to).
                    mine = getattr(self.session, 'user_id', '')
                    owner = event_owner(e)
                    if mine and owner and owner != mine:
                        return False
                    self.event = e
                    return True
        return False

    def run(self) -> None:
        '''Main loop, continuously checks for new gateway events and properly handles them.'''
        self.is_ready = True
        connected_at = time()
        fast_fails = 0
        while not self.stop_event.is_set():
            try:
                response = self.session.receive_event()
            except Exception as e:
                if self.stop_event.is_set():
                    break
                debugger.log(e, f'{self.name} - run - reconnection')
                self.is_ready = False
                detail = e.describe() if isinstance(e, GatewayClosed) else type(e).__name__
                self.menu.notify(f'[!] Connection lost ({detail}), attempting to reconnect...', NotificationPriority.HIGH)

                #Refused for good (invalid token, invalid intents...): retrying only spams Discord with logins
                if isinstance(e, GatewayClosed) and e.fatal:
                    self.session.fatal_close = e.describe()
                    self.menu.notify(
                        f'[E] Discord refused the login: {e.describe()}. Not retrying, fix it and restart.',
                        NotificationPriority.VERY_HIGH
                    )
                    self.menu.kill()
                    print(f'[E] Discord refused the login: {e.describe()}', flush=True)
                    break

                #Dropped again right after logging in several times in a row: stop instead of looping forever
                fast_fails = fast_fails + 1 if time() - connected_at < 20 else 1
                if fast_fails >= 4:
                    self.session.fatal_close = f'dropped {fast_fails}x right after login ({detail})'
                    self.menu.notify(
                        f'[E] Connection dropped {fast_fails} times right after login ({detail}). Stopping to protect the account.',
                        NotificationPriority.VERY_HIGH
                    )
                    self.menu.kill()
                    print(f'[E] Connection dropped {fast_fails} times right after login: {detail}', flush=True)
                    break

                reconnected = False
                last_error = e
                for attempt in range(1, 9):
                    if self.stop_event.is_set():
                        break
                    delay = min(60, 2 ** attempt)
                    self.menu.notify(f'[*] Reconnect attempt {attempt}/8 in {delay}s.')
                    if self.stop_event.wait(delay):
                        break
                    try:
                        if self.session.reconnect():
                            reconnected = True
                            break
                    except Exception as reconnect_error:
                        last_error = reconnect_error
                        debugger.log(reconnect_error, f'{self.name} - reconnect attempt {attempt}')
                if self.stop_event.is_set():
                    break
                if reconnected:
                    self.is_ready = True
                    connected_at = time()
                    self.menu.last_catch_at = connected_at
                    self.menu.notify('[*] Reconnection succeeded.')
                    continue
                self.menu.notify(
                    f'[E] Reconnection failed after 8 attempts: {type(last_error).__name__}. Manual intervention required.',
                    NotificationPriority.VERY_HIGH
                )
                self.menu.kill()
                print(f'[E] Reconnection failed. Exception: {last_error}', flush=True)
                break

            if not self.check_event(response):
                #Invalid/Irrelevant gateway event
                continue

            try:
                self.message.make(self.event)
            except Exception as e:
                debugger.log(e, f'{self.name} - run - message.make')
                dump_event(self.event, 'parse_errors.jsonl')
                self.menu.notify(f'[!] Failed to parse message: {e}', NotificationPriority.LOW)
                continue

            #Track asynchronous purchase responses and explicit auto-sell receipts.
            try:
                raw_purchase_text = ' '.join(filter(None, [
                    self.message.title, self.message.description,
                    self.message.content, self.message.untitled
                ]))
                if self.sch:
                    self.sch.inspect_purchase_message(raw_purchase_text)
                self.menu.session_stats.record_sell_text(raw_purchase_text)
            except Exception as e:
                debugger.log(e, f'{self.name} - purchase/sell tracking')

            #Boost ended detection ("Your fishing boost ended!" / "Your treasure boost ended!")
            try:
                raw = ' '.join(filter(None, [
                    self.message.title, self.message.description,
                    self.message.content, self.message.untitled
                ])).lower()
                if self.sch and 'boost' in raw and 'ended' in raw:
                    if 'treasure' in raw:
                        self.sch.boost_ended('treasure')
                        continue
                    elif 'fish' in raw:
                        self.sch.boost_ended('fish')
                        continue
            except Exception as e:
                debugger.log(e, f'{self.name} - run - boost_ended')

            #Safety valve: a fish result means any previous (false) captcha detection is over
            if self.captcha.detected and self.message.title and self.category.fish in self.message.title:
                self.captcha.reset()

            if self.captcha.detected and not self.captcha.regenerating:
                if 'You may now continue' in self.message.text:
                    #Captcha bypassed
                    self.menu.rcv_bypasses += 1
                    self.captcha.reset()
                    self.menu.last_catch_at = time()
                    self.menu.notify('[*] Captcha bypassed !')
                else:
                    if 'Incorrect code' in self.message.text:
                        self.menu.notify('[*] Incorrect code.', NotificationPriority.LOW)
                        continue
                    else:
                        #Message sent by the bot while captcha is detected (ignored)
                        debugger.log(self, f'{self.name} - run (Message sent by the AFB while captcha is detected)')
                        continue
            elif self.captcha.regenerating:
                if self.captcha.detect(self.event):
                    self.captcha.solve()
                    continue
                else:
                    #No detection while regenerating (unrelated message), keep waiting
                    debugger.log(self, f'{self.name} - run (No detection while regenerating)')
                    continue
            else:
                if self.captcha.detect(self.event):
                    self.menu.notify('[!] Captcha detected !', NotificationPriority.NORMAL)
                    dump_event(self.event, 'captcha_events.jsonl', 10)
                    self.captcha.solve()
                    continue
                else:
                    if self.message.title:
                        #Normal messages
                        if self.category.fish in self.message.title:
                            #Fish (/fish or button) messages
                            self.menu.items = self.message.build()
                            self.menu.rcv_streak += 1
                            self.menu.rcv_fish += 1
                            self.menu.log_catch(self.menu.items)
                        elif self.message.title.find(self.category.profile) > -1:
                            #Profile (/profile) messages
                            self.profile.update(self.message.description)
                            balance_value = self.menu.session_stats.parse_money(self.profile.balance)
                            if balance_value is not None:
                                self.menu.session_stats.record_balance(balance_value)
                            self.menu.notify('[*] Profile updated.')
                        elif self.message.title.find(self.category.charms) > -1:
                            #Charms (/charms) messages
                            self.profile.charms.update(self.message.description)
                            self.menu.notify('[*] Charms updated.')
                        elif self.message.title.find(self.category.buffs) > -1:
                            #Buffs/multipliers (/buffs) messages
                            self.profile.buffs.update(self.message.description)
                            self.menu.notify('[*] Buffs updated.')
                        elif self.message.title.find(self.category.quests) > -1:
                            #Quest list (/quests) messages
                            self.profile.quests.update(self.message.description)
                            self.menu.notify('[*] Quests updated.')
                        elif self.message.title.find(self.category.leaderboard) > -1:
                            #Leaderboard (/pos) messages
                            self.profile.leaderboard.update(self.message.description)
                            self.menu.notify('[*] Leaderboards updated.')
                        else:
                            #Unhandled titled message
                            dump_event(self.event)
                            self.menu.notify(f'{sanitize(self.message.title)}: {sanitize(self.message.description)}')
                    else:
                        #Untitled messages
                        if self.message.content:
                            if self.message.content.find('You must wait') > -1:
                                #Intentional short cooldown
                                self.menu.notify(
                                    '[*] If automatic, this short cooldown is intentional to ensure non-bot behavior.',
                                    NotificationPriority.VERY_LOW
                                )
                            else:
                                dump_event(self.event)
                                self.menu.notify(f'[*] {self.message.untitled}')
                        else:
                            #Untitled with empty content - probably embed only with description
                            dump_event(self.event)
                            if self.menu.is_alive:
                                self.menu.notify(f'[*] {self.message.untitled}')
                            else:
                                if (self.message.untitled or '').find('You hired a worker for the next') > -1 \
                                    or (self.message.untitled or '').find('You already have a worker working') > -1:
                                    print(f'[*] {self.message.untitled}', flush=True)

        self.is_ready = False


@dataclass(slots=True)
class Dispatcher:
    '''Dispatcher class, responsible for making and sending fish & verification commands.'''
    #Pointers
    session: DiscordWrapper
    config: ConfigManager
    menu: BaseMenu
    sch: Scheduler
    rcv: Receiver
    captcha: Captcha = field(init=False)
    message: Message = field(init=False)
    stop_event: Event = field(default_factory=Event, repr=False)

    #Objects
    cooldown: CooldownManager = field(init=False)

    #Flags
    in_cooldown: bool = False
    paused: bool = False

    def __post_init__(self) -> None:
        self.captcha = self.rcv.captcha
        self.message = self.rcv.message
        self.cooldown = CooldownManager(user_cooldown=self.config.user_cooldown)

    def make_command(self, cmd: str, name: str, value: str, type: int = 3) -> tuple:
        '''Builds a tuple containing the command (str) and the 'options' parameter (dict).'''
        parameters = {
            'type': type,
            'name': name,
            'value': value
        }
        return (cmd, parameters)

    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'

    def toggle_pause(self) -> bool:
        '''Play/Pause switch. Returns the new paused state.'''
        if self.sch.status == SchStatus.BREAK:
            self.sch.interrupt_break()
        if self.paused:
            self.paused = False
            self.menu.last_catch_at = time()
            self.menu.notify('[*] Autofishbot resumed.')
        else:
            self.paused = True
            self.menu.notify('[*] Autofishbot paused.')
        return self.paused

    def pause(self) -> bool:
        '''Alias for toggle_pause() kept for backwards compatibility.'''
        return self.toggle_pause()

    @property
    def timeout(self) -> float:
        '''Timeout cooldown for general commands (other than fish commands).'''
        return max(1.0, self.cooldown.custom(
            mu=uniform(3, 5),
            sigma=random()
        ))

    def run(self) -> None:
        '''Main loop, sends commands.'''
        _delay = 0.2

        while not self.stop_event.is_set():
            if not self.rcv.is_ready:
                self.stop_event.wait(_delay)
                continue

            if self.captcha.detected and not self.captcha.regenerating:
                if self.captcha.solving or len(self.captcha.answers) > 0:
                    try:
                        answer = self.captcha.answers.pop()
                        self.menu.notify(f'[!] Attempting code: "{answer}".')
                        cmd, param = self.make_command('verify', 'answer', answer)
                        self.session.request(command=cmd, parameters=param, category=COMMAND)

                        self.stop_event.wait(self.timeout)
                    except IndexError:
                        continue
                else:
                    if self.captcha.regens < MAX_CAPTCHA_REGENS:
                        self.captcha.regens += 1
                        self.menu.notify(
                            f'[!] Regenerating captcha ({self.captcha.regens + 1}/{MAX_CAPTCHA_REGENS})',
                            NotificationPriority.HIGH
                        )
                        self.captcha.regenerating = True

                        cmd, param = self.make_command('verify', 'answer', 'regen')
                        self.session.request(command=cmd, parameters=param, category=COMMAND)
                        self.stop_event.wait(1.0)
                    else:
                        self.menu.notify(
                            '[!] MAXIMUM CAPTCHA REGENS EXCEEDED, WAITING FOR MANUAL INPUT !',
                            NotificationPriority.VERY_HIGH
                        )
                        while self.captcha.detected and not self.stop_event.is_set():
                            self.stop_event.wait(1.0)
            else:
                while (
                    not self.stop_event.is_set()
                    and (
                        self.captcha.busy
                        or self.captcha.regenerating
                        or self.sch.status in (SchStatus.BUSY, SchStatus.BREAK)
                    )
                ):
                    self.stop_event.wait(_delay)

                if self.stop_event.is_set():
                    break

                if not self.captcha.detected and not self.paused and self.menu.is_alive:
                    _start = time()

                    if self.message.id and self.message.play_id:
                        if self.session.request(message_id=self.message.id, custom_id=self.message.play_id, category=BUTTON):
                            pass
                        else:
                            #Failed interaction, reset ids to trigger slash command fallback
                            self.message.reset_ids()
                    else:
                        self.session.request(command='fish', category=COMMAND)

                    self.in_cooldown = True
                    wait_for = max(0.2, self.cooldown.new() - (time() - _start))
                    self.stop_event.wait(wait_for)
                    self.in_cooldown = False
                else:
                    self.stop_event.wait(_delay)


#------------------------ INIT --------------------------#
if __name__ == '__main__':
    from headless import configure_line_buffering, wants_headless, strip_flags, HeadlessMenu, run_headless

    headless = wants_headless(sys.argv)
    strip_flags(sys.argv)
    if headless:
        configure_line_buffering()

    print('\n[*] Starting...', flush=True)

    #Loads config
    config = ConfigManager()

    #Setup debugger
    debugger.setup(config.debug)

    #Instantiate menu
    if headless:
        menu = HeadlessMenu()
    elif config.compact_mode:
        menu = CompactMenu()
    else:
        menu = MainMenu()

    notifier = NotificationManager(config, menu)
    menu.notifier = notifier

    #Instantiate session
    try:
        session = DiscordWrapper(
            config=config,
            menu=menu,
            auto_connect=True
        )
    except SystemExit as e:
        if headless and e.code not in (None, 0):
            print(e.code, flush=True)
            #Fatal auth errors (HTTP 401/403) should not cause a Pterodactyl restart loop
            sys.exit(0 if ('401' in str(e.code) or '403' in str(e.code)) else 1)
        raise
    except GatewayClosed as e:
        print(f'[E] {e.describe()}', flush=True)
        if headless and e.fatal:
            sys.exit(0)
        sys.exit(1)
    except Exception as e:
        print(f'[E] Failed to connect: {e}', flush=True)
        sys.exit(1)

    stop_event = Event()

    #Instantiate receiver
    receiver = Receiver(
        session=session,
        config=config,
        menu=menu,
        stop_event=stop_event
    )

    #Instantiate Scheduler
    scheduler = Scheduler(
        session=session,
        config=config,
        menu=menu,
        captcha=receiver.captcha,
        stop_event=stop_event
    )

    receiver.sch = scheduler

    #Instantiate dispatcher
    dispatcher = Dispatcher(
        session=session,
        config=config,
        menu=menu,
        sch=scheduler,
        rcv=receiver,
        stop_event=stop_event
    )

    #Async flow
    rcv_thread = Thread(target=receiver.run, daemon=True, name='Receiver')
    sch_thread = Thread(target=scheduler.run, args=(dispatcher,), daemon=True, name='Scheduler')
    dsp_thread = Thread(target=dispatcher.run, daemon=True, name='Dispatcher')

    rcv_thread.start()
    sch_thread.start()
    dsp_thread.start()

    #Optional 2-way Telegram remote control, anti-stuck watchdog & lightweight Web Dashboard
    from app.control import ControlHub, ManagedAccount
    from app.telegram_remote import TelegramRemote
    from app.watchdog import StuckWatchdog
    from app.web_dashboard import WebDashboard

    acc_name = config._selected_config[:-len('.config')] if config._selected_config.endswith('.config') else (config._selected_config or 'default')
    managed = ManagedAccount(
        name=acc_name,
        config=config,
        menu=menu,
        session=session,
        dispatcher=dispatcher,
        scheduler=scheduler,
        profile=receiver.profile,
        is_dead_fn=lambda: not (rcv_thread.is_alive() and sch_thread.is_alive() and dsp_thread.is_alive()),
    )
    hub = ControlHub([managed])
    watchdog = StuckWatchdog.from_env(hub, stop_event=stop_event)
    if watchdog:
        watchdog.start()
    tg_remote = TelegramRemote.from_configs([config], hub, stop_event=stop_event)
    if tg_remote:
        tg_remote.start()
    web_dashboard = WebDashboard.from_configs([config], hub)
    if web_dashboard:
        web_dashboard.start()

    #Start menu
    exit_code = 0
    try:
        if headless:
            run_headless(
                menu=menu,
                config=config,
                dispatcher=dispatcher,
                profile=receiver.profile,
                scheduler=scheduler,
                threads=[rcv_thread, sch_thread, dsp_thread]
            )
        else:
            menu.run(
                config=config,
                dispatcher=dispatcher,
                profile=receiver.profile,
                scheduler=scheduler,
                threads=[rcv_thread, sch_thread, dsp_thread]
            )
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if isinstance(e.code, str):
            print(e.code, flush=True)
    finally:
        stop_event.set()
        if watchdog:
            watchdog.stop()
        if tg_remote:
            tg_remote.stop()
        if web_dashboard:
            web_dashboard.stop()
        menu.kill()
        if config.fish_on_exit and menu.rcv_streak > 0 and not session.fatal_close:
            try:
                cmd, data = scheduler.commands.worker.data
                session.request(command=cmd, parameters=data)
                sleep(3)
            except Exception:
                pass

        menu.notify('[*] Autofishbot stopped.')
        notifier.flush()
        session.disconnect()

    if headless:
        sys.exit(exit_code)
    exit('\n[!] User exited.')
