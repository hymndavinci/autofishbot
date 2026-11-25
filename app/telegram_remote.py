from __future__ import annotations

from os import environ
from threading import Event, Thread
from requests import Session

from .control import ControlHub, TOGGLE_FEATURES
from .utils import debugger

TG_HELP = (
    'Autofishbot Remote Control\n'
    '/menu [akun] - panel tombol interaktif (paling praktis di HP)\n'
    '/status - ringkasan semua akun\n'
    '/stats [akun] - detail statistik & tangkapan terakhir\n'
    '/logs [jumlah] [akun] - lihat log terakhir (default 20, max 60)\n'
    '/accounts - daftar akun\n'
    '/pause [akun|all] - pause akun\n'
    '/resume [akun|all] - lanjutkan akun\n'
    '/sell [akun] - jadwalkan /sell\n'
    '/daily [akun] - jadwalkan /daily\n'
    '/inv [akun] - update inventory (/profile)\n'
    '/fishboost [akun] - beli More Fish boost\n'
    '/treasureboost [akun] - beli More Treasures boost\n'
    '/toggle <1-7|fitur> [akun] - ubah toggle otomasi\n'
    '/save [akun] - simpan toggle ke file config\n'
    '/help - tampilkan menu ini'
)


class TelegramRemote:
    '''Two-way Telegram Bot command + inline keyboard listener with strict chat_id authorization.'''

    def __init__(self, bot_token: str, allowed_chat_ids: set[str], hub: ControlHub,
                 stop_event: Event | None = None) -> None:
        self.bot_token = bot_token.strip()
        self.allowed_chat_ids = {str(cid).strip() for cid in allowed_chat_ids if str(cid).strip()}
        self.hub = hub
        self.stop_event = stop_event or Event()
        self._http = Session()
        self._base = f'https://api.telegram.org/bot{self.bot_token}'
        self._offset: int | None = None
        self._thread: Thread | None = None

    @classmethod
    def from_configs(cls, configs: list, hub: ControlHub, stop_event: Event | None = None) -> TelegramRemote | None:
        '''Builds a single TelegramRemote from the first config that has Telegram credentials.'''
        flag = environ.get('AFB_TG_REMOTE', '1').strip().lower()
        if flag in ('0', 'false', 'off', 'no'):
            return None

        token = ''
        chat_ids: set[str] = set()
        for cfg in configs:
            t = str(getattr(cfg, 'telegram_bot_token', '') or '').strip()
            c = str(getattr(cfg, 'telegram_chat_id', '') or '').strip()
            if not t or not c:
                continue
            if not token:
                token = t
            if t == token:
                for part in c.split(','):
                    if part.strip():
                        chat_ids.add(part.strip())
        if not token or not chat_ids:
            return None
        return cls(token, chat_ids, hub, stop_event=stop_event)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(target=self._loop, daemon=True, name='Telegram remote')
        self._thread.start()
        print('[*] Telegram 2-way remote control enabled (use /menu for buttons).', flush=True)

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self._http.close()
        except Exception:
            pass

    def _send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        payload: dict = {'chat_id': chat_id, 'text': str(text)[:3900]}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        try:
            self._http.post(f'{self._base}/sendMessage', json=payload, timeout=10)
        except Exception as e:
            debugger.log(e, 'TelegramRemote - send')

    def _edit(self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload: dict = {'chat_id': chat_id, 'message_id': message_id, 'text': str(text)[:3900]}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        try:
            self._http.post(f'{self._base}/editMessageText', json=payload, timeout=10)
        except Exception as e:
            debugger.log(e, 'TelegramRemote - edit')

    def _answer_callback(self, callback_id: str, text: str = '') -> None:
        try:
            self._http.post(
                f'{self._base}/answerCallbackQuery',
                json={'callback_query_id': callback_id, 'text': str(text)[:180]},
                timeout=8,
            )
        except Exception as e:
            debugger.log(e, 'TelegramRemote - answerCallback')

    def _drain_backlog(self) -> None:
        '''Skips messages/callbacks sent before startup so stale commands do not execute on boot.'''
        try:
            resp = self._http.get(
                f'{self._base}/getUpdates',
                params={'timeout': 0, 'allowed_updates': '["message","callback_query"]'},
                timeout=8,
            )
            if resp.status_code == 200:
                updates = resp.json().get('result') or []
                if updates:
                    self._offset = int(updates[-1]['update_id']) + 1
        except Exception as e:
            debugger.log(e, 'TelegramRemote - drain_backlog')

    def _loop(self) -> None:
        self._drain_backlog()
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                params = {'timeout': 25, 'allowed_updates': '["message","callback_query"]'}
                if self._offset is not None:
                    params['offset'] = self._offset
                resp = self._http.get(f'{self._base}/getUpdates', params=params, timeout=32)
                if resp.status_code == 409:
                    debugger.log('Telegram 409 Conflict on getUpdates', 'TelegramRemote')
                    self.stop_event.wait(min(60.0, backoff * 4))
                    backoff = min(30.0, backoff * 1.5)
                    continue
                if resp.status_code != 200:
                    self.stop_event.wait(backoff)
                    backoff = min(30.0, backoff * 1.5)
                    continue

                backoff = 2.0
                data = resp.json()
                for update in data.get('result') or []:
                    upd_id = update.get('update_id')
                    if upd_id is not None:
                        self._offset = int(upd_id) + 1

                    cb = update.get('callback_query')
                    if cb:
                        self._process_callback(cb)
                        continue

                    msg = update.get('message') or {}
                    chat_id = str((msg.get('chat') or {}).get('id', '')).strip()
                    text = str(msg.get('text') or '').strip()
                    if not chat_id or not text or chat_id not in self.allowed_chat_ids:
                        continue
                    if text.startswith('/'):
                        reply_text, markup = self.handle_command_with_markup(text)
                        if reply_text:
                            self._send(chat_id, reply_text, reply_markup=markup)
            except Exception as e:
                if self.stop_event.is_set():
                    break
                debugger.log(e, 'TelegramRemote - poll')
                self.stop_event.wait(backoff)
                backoff = min(30.0, backoff * 1.5)

    #------------------------ INLINE MENU BUILDERS --------------------------#
    def _clamp_idx(self, idx: int) -> int:
        if not self.hub.accounts:
            return 0
        return max(0, min(idx, len(self.hub.accounts) - 1))

    def _account_idx(self, selector: str | None) -> int:
        acc = self.hub.resolve(selector)
        if acc is None:
            return 0
        for i, item in enumerate(self.hub.accounts):
            if item is acc:
                return i
        return 0

    def render_menu(self, idx: int = 0, view: str = 'main') -> tuple[str, dict]:
        '''Builds (text, inline_keyboard_markup) for interactive /menu views.'''
        if not self.hub.accounts:
            return 'No active accounts.', {'inline_keyboard': []}

        idx = self._clamp_idx(idx)
        acc = self.hub.accounts[idx]
        s = acc.snapshot()
        total = len(self.hub.accounts)
        user = f' ({s["username"]})' if s['username'] else ''
        bal_hr = f'${s["balance_per_hour"]:,.2f}/h' if s['balance_per_hour'] is not None else '-'

        if view == 'toggles':
            text = (
                f'Automation Toggles — [{idx + 1}/{total}] {s["name"]}{user}\n'
                f'State: {s["state"]}\n'
                f'Tap tombol di bawah untuk ON/OFF:'
            )
            rows = []
            pair = []
            for key, feat, label in TOGGLE_FEATURES:
                on = bool(s['toggles'].get(feat))
                btn = {'text': f'{"✅" if on else "⬜"} {label}', 'callback_data': f't:{idx}:{key}'}
                pair.append(btn)
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
            if pair:
                rows.append(pair)
            rows.append([
                {'text': '💾 Save Config', 'callback_data': f'act:{idx}:save:toggles'},
                {'text': '⬅ Back', 'callback_data': f'v:{idx}:main'},
            ])
            return text, {'inline_keyboard': rows}

        if view == 'logs':
            logs = acc.recent_logs(15)
            body = '\n'.join(f'[{r["time"]}] {r["text"]}' for r in logs) or '(belum ada log)'
            text = f'Live Logs (last {len(logs)}) — {s["name"]}{user}\n\n{body}'
            rows = [[
                {'text': '🔄 Refresh Logs', 'callback_data': f'v:{idx}:logs'},
                {'text': '⬅ Back', 'callback_data': f'v:{idx}:main'},
            ]]
            return text, {'inline_keyboard': rows}

        #Main view
        notif = s['error'] or s['notification'] or '-'
        text = (
            f'Autofishbot Control — [{idx + 1}/{total}] {s["name"]}{user}\n'
            f'State: {s["state"]} | Up: {s["uptime"]}\n'
            f'Fish: {s["fish"]} ({s["rate_per_min"]:.1f}/m | {s["catch_per_hour"]:.0f}/h)\n'
            f'Streak: {s["streak"]} | Bypasses: {s["bypasses"]} | Watchdog: {s["watchdog_recoveries"]}\n'
            f'Balance: {s["balance"]} ({bal_hr}) | Sell: ${s["auto_sell_total"]:,.2f}\n'
            f'Info: {notif}'
        )
        pause_btn = (
            {'text': '▶ Resume', 'callback_data': f'act:{idx}:resume:main'}
            if s['paused']
            else {'text': '⏸ Pause', 'callback_data': f'act:{idx}:pause:main'}
        )
        rows = [
            [{'text': '🔄 Refresh', 'callback_data': f'v:{idx}:main'}, pause_btn],
            [
                {'text': '💰 Sell', 'callback_data': f'act:{idx}:sell:main'},
                {'text': '🎁 Daily', 'callback_data': f'act:{idx}:daily:main'},
                {'text': '🎒 Inv', 'callback_data': f'act:{idx}:inventory:main'},
            ],
            [
                {'text': '🐟 Fish Boost', 'callback_data': f'act:{idx}:more_fish:main'},
                {'text': '💎 Treasure Boost', 'callback_data': f'act:{idx}:more_treasures:main'},
            ],
            [
                {'text': '⚙ Toggles', 'callback_data': f'v:{idx}:toggles'},
                {'text': '📜 Logs', 'callback_data': f'v:{idx}:logs'},
            ],
        ]
        if total > 1:
            prev_idx = (idx - 1) % total
            next_idx = (idx + 1) % total
            rows.append([
                {'text': '◀ Prev', 'callback_data': f'v:{prev_idx}:main'},
                {'text': f'Akun {idx + 1}/{total}', 'callback_data': f'v:{idx}:main'},
                {'text': 'Next ▶', 'callback_data': f'v:{next_idx}:main'},
            ])
        return text, {'inline_keyboard': rows}

    def handle_callback(self, data: str) -> tuple[str, str, dict]:
        '''Processes inline button callback_data. Returns (toast_msg, new_text, new_markup).'''
        parts = (data or '').split(':')
        if len(parts) < 3:
            text, markup = self.render_menu(0, 'main')
            return 'Invalid action', text, markup

        kind = parts[0]
        try:
            idx = self._clamp_idx(int(parts[1]))
        except ValueError:
            idx = 0

        if not self.hub.accounts:
            return 'No active accounts', 'No active accounts.', {'inline_keyboard': []}

        acc = self.hub.accounts[idx]
        if kind == 'v':
            view = parts[2]
            text, markup = self.render_menu(idx, view)
            return 'Updated', text, markup

        if kind == 't':
            key = parts[2]
            _, toast = acc.toggle_feature(key)
            text, markup = self.render_menu(idx, 'toggles')
            return toast, text, markup

        if kind == 'act':
            action = parts[2]
            view = parts[3] if len(parts) > 3 else 'main'
            _, toast = acc.trigger_action(action)
            text, markup = self.render_menu(idx, view)
            return toast, text, markup

        text, markup = self.render_menu(idx, 'main')
        return 'Unknown action', text, markup

    def _process_callback(self, cb: dict) -> None:
        cb_id = str(cb.get('id', ''))
        msg = cb.get('message') or {}
        chat_id = str((msg.get('chat') or {}).get('id', '')).strip()
        msg_id = msg.get('message_id')
        data = str(cb.get('data') or '')
        if not chat_id or chat_id not in self.allowed_chat_ids:
            if cb_id:
                self._answer_callback(cb_id, 'Unauthorized')
            return
        toast, text, markup = self.handle_callback(data)
        if cb_id:
            self._answer_callback(cb_id, toast)
        if msg_id is not None:
            self._edit(chat_id, int(msg_id), text, reply_markup=markup)

    #------------------------ TEXT COMMANDS --------------------------#
    def handle_command(self, raw: str) -> str:
        text, _ = self.handle_command_with_markup(raw)
        return text

    def handle_command_with_markup(self, raw: str) -> tuple[str, dict | None]:
        parts = raw.strip().split()
        if not parts:
            return '', None
        cmd = parts[0].split('@', 1)[0].lower()
        args = parts[1:]

        if cmd == '/menu':
            idx = self._account_idx(args[0] if args else None)
            return self.render_menu(idx, 'main')

        if cmd in ('/help', '/start'):
            return TG_HELP, None

        if cmd == '/logs':
            limit = 20
            selector = None
            if args:
                if args[0].isdigit():
                    limit = max(1, min(int(args[0]), 60))
                    if len(args) > 1:
                        selector = args[1]
                else:
                    selector = args[0]
                    if len(args) > 1 and args[1].isdigit():
                        limit = max(1, min(int(args[1]), 60))
            acc = self.hub.resolve(selector)
            if not acc:
                return f'Account "{selector}" not found. Use /accounts.', None
            rows = acc.recent_logs(limit)
            if not rows:
                return f'[{acc.name}] Belum ada log.', None
            body = '\n'.join(f'[{r["time"]}] {r["text"]}' for r in rows)
            return f'Logs [{acc.name}] (last {len(rows)}):\n{body}', None

        if cmd == '/accounts':
            lines = ['Accounts:']
            for i, acc in enumerate(self.hub.accounts, 1):
                snap = acc.snapshot()
                user = f' ({snap["username"]})' if snap['username'] else ''
                lines.append(f'{i}. {snap["name"]}{user} — {snap["state"]}')
            return '\n'.join(lines), None

        if cmd == '/status':
            lines = []
            for i, acc in enumerate(self.hub.accounts, 1):
                s = acc.snapshot()
                bal_hr = f'${s["balance_per_hour"]:,.2f}/h' if s['balance_per_hour'] is not None else '-'
                lines.append(
                    f'[{i}:{s["name"]}] {s["state"]}\n'
                    f'  Fish: {s["fish"]} ({s["rate_per_min"]:.1f}/m | {s["catch_per_hour"]:.0f}/h)\n'
                    f'  Streak: {s["streak"]} | Bypasses: {s["bypasses"]} | Up: {s["uptime"]}\n'
                    f'  Balance: {s["balance"]} ({bal_hr}) | Auto-sell: ${s["auto_sell_total"]:,.2f}'
                )
            return ('\n\n'.join(lines) if lines else 'No active accounts.'), None

        if cmd == '/stats':
            acc = self.hub.resolve(args[0] if args else None)
            if not acc:
                return f'Account "{args[0]}" not found. Use /accounts.', None
            s = acc.snapshot()
            toggles_str = '\n'.join(
                f'  [{k}] {label}: {"ON" if s["toggles"].get(feat) else "off"}'
                for k, feat, label in TOGGLE_FEATURES
            )
            recent = '\n'.join(f'  {r["time"]} {r["text"]}' for r in s['recent_catches'][:5]) or '  (none yet)'
            bal_hr = f'${s["balance_per_hour"]:,.2f}/h' if s['balance_per_hour'] is not None else '-'
            return (
                f'Account: {s["name"]}' + (f' ({s["username"]})' if s['username'] else '') + '\n'
                f'State: {s["state"]} | Uptime: {s["uptime"]} | Watchdog: {s["watchdog_recoveries"]}\n'
                f'Fish: {s["fish"]} ({s["rate_per_min"]:.1f}/min, {s["catch_per_hour"]:.0f}/hr)\n'
                f'Streak: {s["streak"]} | Bypasses: {s["bypasses"]} | Queue: {s["queue_len"]}\n'
                f'Balance: {s["balance"]} | Bal/hr: {bal_hr}\n'
                f'Auto-sell: ${s["auto_sell_total"]:,.2f} ({s["auto_sell_count"]}x)\n'
                f'Toggles:\n{toggles_str}\n'
                f'Recent catches:\n{recent}'
            ), None

        if cmd in ('/pause', '/resume'):
            want_pause = cmd == '/pause'
            target = args[0].lower() if args else 'all'
            if target == 'all':
                results = [acc.set_paused(want_pause)[1] for acc in self.hub.accounts]
                return '\n'.join(results), None
            acc = self.hub.resolve(target)
            if not acc:
                return f'Account "{target}" not found. Use /accounts.', None
            return acc.set_paused(want_pause)[1], None

        if cmd == '/toggle':
            if not args:
                options = ', '.join(f'{k}={feat}' for k, feat, _ in TOGGLE_FEATURES)
                return f'Usage: /toggle <1-7|feature> [account]\nOptions: {options}', None
            feature = args[0]
            acc = self.hub.resolve(args[1] if len(args) > 1 else None)
            if not acc:
                return f'Account "{args[1]}" not found. Use /accounts.', None
            _, msg = acc.toggle_feature(feature)
            return msg, None

        action_cmds = {
            '/sell': 'sell',
            '/daily': 'daily',
            '/inv': 'inventory',
            '/inventory': 'inventory',
            '/fishboost': 'more_fish',
            '/treasureboost': 'more_treasures',
            '/save': 'save',
        }
        if cmd in action_cmds:
            acc = self.hub.resolve(args[0] if args else None)
            if not acc:
                return f'Account "{args[0]}" not found. Use /accounts.', None
            _, msg = acc.trigger_action(action_cmds[cmd])
            return msg, None

        return f'Unknown command "{cmd}". Send /menu or /help.', None
