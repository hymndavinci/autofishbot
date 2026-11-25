from __future__ import annotations

from datetime import datetime, timezone
from json import dumps
from pathlib import Path
from queue import Queue, Empty, Full
from re import sub
from threading import Thread, Lock
from time import sleep, time
from requests import Session

#Images used by the Discord embeds (assets/images/). Replace the files to use your own pictures:
#  cat.png    - small symbol shown beside regular notifications
#  banner.png - 16:9 banner shown above important notifications
_ASSET_DIR = Path(__file__).resolve().parent.parent / 'assets' / 'images'
_ICON_NAME = 'cat.png'
_BANNER_NAME = 'banner.png'
_asset_cache: dict[str, bytes | None] = {}


def _load_asset(name: str) -> bytes | None:
    '''Reads an embed image once; returns None when the file is missing (embeds then go without it).'''
    if name not in _asset_cache:
        try:
            _asset_cache[name] = (_ASSET_DIR / name).read_bytes()
        except OSError:
            _asset_cache[name] = None
    return _asset_cache[name]


class NotificationManager:
    '''Best-effort asynchronous Telegram and Discord notification delivery.'''

    #Text-card side-bar colours: pale pink by default, a soft tint only for things that need you.
    _LEVELS = {
        'ok':      0xF7D6DE,  #pale pink
        'info':    0xF7D6DE,
        'warn':    0xF0DDB0,  #soft sand
        'alert':   0xE58F9F,  #rose
        'stopped': 0xD6D6DA,  #light grey
    }
    _BANNER_BAR = 0xFFFFFF    #bar colour of the banner embed (white)
    _MARKS = {'ok': '✿', 'info': '✿', 'warn': '✦', 'alert': '✦', 'stopped': '✧'}
    #Titles that get the 16:9 banner in addition to the alert/stopped levels (everything else stays compact)
    _BANNER_TITLES = ('Connected', 'Back online')

    # (keyword, embed title, level, status text) — first match wins, most specific first
    _EMBED_STYLES = [
        ('maximum captcha regens exceeded', 'Captcha needs you',   'alert',   'Action needed'),
        ('reconnection failed',             'Reconnection failed', 'alert',   'Action needed'),
        ('manual intervention',             'Manual intervention', 'alert',   'Action needed'),
        ('manual input',                    'Captcha needs you',   'alert',   'Action needed'),
        ('connection lost',                 'Connection lost',     'alert',   'Offline'),
        ('reconnection succeeded',          'Back online',         'ok',      'Online'),
        ('connected to discord',            'Connected',           'ok',      'Online'),
        ('captcha bypassed',                'Captcha solved',      'ok',      'Resolved'),
        ('captcha detected',                'Captcha detected',    'warn',    'Attention'),
        ('incorrect code',                  'Incorrect code',      'warn',    'Attention'),
        ('buy failed',                      'Purchase failed',     'warn',    'Retrying'),
        ('watchdog:',                       'Watchdog recovery',   'warn',    'Recovering'),
        ('proxy error',                     'Proxy error',         'warn',    'Attention'),
        ('emerald fish',                    'Emerald fish',        'ok',      'Nice catch'),
        ('boost purchase',                  'Boost purchase',      'info',    'Info'),
        ('autofishbot stopped',             'Stopped',             'stopped', 'Offline'),
        ('stopped',                         'Worker stopped',      'alert',   'Action needed'),
    ]

    def __init__(self, config, menu=None):
        self.config = config
        self.menu = menu
        self.label = '' #Account label shown on external messages (set after login)
        self.queue: Queue[str] = Queue(maxsize=100)
        self.started = False
        self.start_lock = Lock()
        self._http = Session()

    def flush(self, timeout: float = 5.0) -> None:
        '''Best-effort wait until queued notifications are delivered (or timeout).'''
        deadline = time() + timeout
        while self.queue.unfinished_tasks > 0 and time() < deadline:
            sleep(0.05)

    def start(self) -> None:
        with self.start_lock:
            if self.started:
                return
            self.started = True
            Thread(target=self._worker, daemon=True, name='External notifications').start()

    def send(self, message: str) -> None:
        if not message:
            return
        if not (
            getattr(self.config, 'discord_webhook_url', '')
            or (getattr(self.config, 'telegram_bot_token', '') and getattr(self.config, 'telegram_chat_id', ''))
        ):
            return
        self.start()
        try:
            self.queue.put_nowait(str(message)[:1800])
        except Full:
            pass

    def _discord_embeds(self, message: str, with_images: bool = True) -> tuple[list, list]:
        '''Builds the stacked Discord embeds for a message: (embeds, attachment file names).'''
        lowered = message.lower()
        title, level, status = 'Notification', 'info', 'Info'
        for term, style_title, style_level, style_status in self._EMBED_STYLES:
            if term in lowered:
                title, level, status = style_title, style_level, style_status
                break
        detail = sub(r'^(\[[^\]]*\]\s*)+', '', message).strip() or message

        use_banner = bool(
            with_images
            and (level in ('alert', 'stopped') or title in self._BANNER_TITLES)
            and _load_asset(_BANNER_NAME)
        )
        use_icon = bool(with_images and not use_banner and _load_asset(_ICON_NAME))

        lines = []
        if self.label:
            lines.append(f'> **account** ‧ {self.label}')
        lines.append(f'> **status** ‧ {status.lower()}')
        card = {
            'title': f'{self._MARKS[level]} {title.lower()}',
            'description': '\n'.join(lines) + f'\n\n⋆ ˚ {detail}',
            'color': self._LEVELS[level],
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'footer': {'text': '✿ autofishbot'},
        }
        embeds, files = [], []
        if use_banner:
            embeds.append({'color': self._BANNER_BAR, 'image': {'url': f'attachment://{_BANNER_NAME}'}})
            files.append(_BANNER_NAME)
        elif use_icon:
            card['thumbnail'] = {'url': f'attachment://{_ICON_NAME}'}
            files.append(_ICON_NAME)
        embeds.append(card)
        return embeds, files

    def _post_discord(self, webhook: str, message: str) -> None:
        '''Sends the embeds with their images attached; falls back to text-only, then to plain text.'''
        embeds, files = self._discord_embeds(message, with_images=True)
        if files:
            response = self._http.post(
                webhook,
                data={'payload_json': dumps({'embeds': embeds})},
                files={f'files[{i}]': (name, _load_asset(name), 'image/png') for i, name in enumerate(files)},
                timeout=8,
            )
            if response.status_code != 400:  #400 = Discord rejected the attachment, retry without images
                response.raise_for_status()
                return
        try:
            payload = {'embeds': self._discord_embeds(message, with_images=False)[0]}
        except Exception:
            payload = {'content': message}
        response = self._http.post(webhook, json=payload, timeout=8)
        response.raise_for_status()

    def _notify_local(self, text: str) -> None:
        if not self.menu:
            return
        try:
            self.menu.notify(text, external=False)
        except TypeError:
            self.menu.notify(text)

    def _deliver(self, message: str) -> None:
        webhook = getattr(self.config, 'discord_webhook_url', '')
        bot_token = getattr(self.config, 'telegram_bot_token', '')
        chat_id = getattr(self.config, 'telegram_chat_id', '')
        if webhook:
            try:
                self._post_discord(webhook, message)
            except Exception as exc:
                self._notify_local(f'[!] Discord notification delivery failed: {type(exc).__name__}')
        if bot_token and chat_id:
            text = f'[{self.label}] {message}' if self.label else message
            for cid in (part.strip() for part in str(chat_id).split(',') if part.strip()):
                try:
                    response = self._http.post(
                        f'https://api.telegram.org/bot{bot_token}/sendMessage',
                        json={'chat_id': cid, 'text': text},
                        timeout=8,
                    )
                    response.raise_for_status()
                except Exception as exc:
                    self._notify_local(f'[!] Telegram notification delivery failed: {type(exc).__name__}')

    def _worker(self) -> None:
        while True:
            try:
                message = self.queue.get(timeout=1)
            except Empty:
                continue
            try:
                self._deliver(message)
            finally:
                self.queue.task_done()
