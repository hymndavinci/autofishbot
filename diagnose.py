#! /usr/bin/env python3
'''
One-shot connection diagnostic. Run it on the SAME host/panel as the bot:

    python diagnose.py                 (uses the first .config in ./configs)
    python diagnose.py akun1.config

On Pterodactyl: set the startup variable PY_FILE=diagnose.py, start the server,
read the output, then set PY_FILE back to autofishbot.py.
The token is never printed.
'''
from __future__ import annotations

import json
import os
import sys
import time
from configparser import ConfigParser

import requests
from websocket import WebSocket, WebSocketConnectionClosedException

API = 'https://discord.com/api/v10'
GATEWAY = 'wss://gateway.discord.gg/?v=10&encoding=json'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'

CLOSE_CODES = {
    1000: 'normal close', 1001: 'going away', 1006: 'abnormal close (network/IP level)',
    4000: 'unknown error', 4001: 'unknown opcode', 4002: 'decode error', 4003: 'not authenticated',
    4004: 'AUTHENTICATION FAILED -> the token is invalid/expired/reset',
    4005: 'already authenticated', 4007: 'invalid seq', 4008: 'RATE LIMITED (too many connects/identifies)',
    4009: 'session timed out', 4010: 'invalid shard', 4011: 'sharding required',
    4012: 'invalid API version', 4013: 'invalid intents', 4014: 'disallowed intents',
}


def say(label: str, ok: bool | None, text: str) -> None:
    mark = {True: 'OK  ', False: 'FAIL', None: 'INFO'}[ok]
    print(f'[{mark}] {label}: {text}', flush=True)


def load_config() -> tuple[dict, str]:
    if len(sys.argv) > 1:
        name = sys.argv[1]
    else:
        files = sorted(f for f in os.listdir('configs') if f.endswith('.config')) if os.path.isdir('configs') else []
        if not files:
            sys.exit('[E] No .config file found in ./configs')
        name = files[0]
    cfg = ConfigParser(interpolation=None)
    cfg.read(os.path.join('configs', name))
    clean = lambda v: (v or '').strip().strip('"\'')
    system = cfg['SYSTEM']
    net = cfg['NETWORK'] if cfg.has_section('NETWORK') else {}
    data = {
        'token': clean(system.get('user_token')),
        'channel_id': clean(system.get('channel_id')),
        'guild_id': clean(system.get('guild_id')),
        'proxy_ip': clean(net.get('proxy_ip')) if net else '',
        'proxy_port': clean(net.get('proxy_port')) if net else '',
        'proxy_user': clean(net.get('proxy_auth_user')) if net else '',
        'proxy_pass': clean(net.get('proxy_auth_password')) if net else '',
    }
    return data, name


def short(resp: requests.Response) -> str:
    body = resp.text[:160].replace('\n', ' ')
    if 'cloudflare' in resp.text.lower() or 'attention required' in resp.text.lower():
        body = 'Cloudflare block page (the host IP is being blocked)'
    return body


def main() -> None:
    cfg, name = load_config()
    say('Config', None, f'{name} | token {"set" if cfg["token"] else "MISSING"} '
                        f'| channel {cfg["channel_id"] or "MISSING"} | guild {cfg["guild_id"] or "MISSING"}')

    proxies = None
    if cfg['proxy_ip'] and cfg['proxy_port']:
        auth = f'{cfg["proxy_user"]}:{cfg["proxy_pass"]}@' if cfg['proxy_user'] and cfg['proxy_pass'] else ''
        url = f'http://{auth}{cfg["proxy_ip"]}:{cfg["proxy_port"]}'
        proxies = {'http': url, 'https': url}
        say('Proxy', None, f'using {cfg["proxy_ip"]}:{cfg["proxy_port"]}')
    else:
        say('Proxy', None, 'none (direct connection from this host)')

    headers = {'User-Agent': UA, 'Authorization': cfg['token']}

    #1. Outbound IP
    try:
        ip = requests.get('https://api.ipify.org', timeout=8, proxies=proxies).text.strip()
        say('Outbound IP', None, ip)
    except Exception as e:
        say('Outbound IP', False, f'{type(e).__name__}: {e}')

    #2. Discord REST reachable?
    try:
        r = requests.get(f'{API}/gateway', timeout=10, proxies=proxies, headers={'User-Agent': UA})
        say('Discord REST (no auth)', r.status_code == 200, f'HTTP {r.status_code} {"" if r.status_code == 200 else short(r)}')
    except Exception as e:
        say('Discord REST (no auth)', False, f'{type(e).__name__}: {e}  -> host cannot reach discord.com')

    #3. Token check
    try:
        r = requests.get(f'{API}/users/@me', timeout=10, proxies=proxies, headers=headers)
        if r.status_code == 200:
            say('Token', True, f'valid (account: {r.json().get("username")})')
        elif r.status_code == 401:
            say('Token', False, 'HTTP 401 -> token invalid/expired/reset. Get a fresh token and update the config.')
        elif r.status_code == 403:
            say('Token', False, f'HTTP 403 -> {short(r)}')
        elif r.status_code == 429:
            say('Token', False, f'HTTP 429 rate limited, retry_after={r.json().get("retry_after")}s')
        else:
            say('Token', False, f'HTTP {r.status_code} {short(r)}')
    except Exception as e:
        say('Token', False, f'{type(e).__name__}: {e}')

    #4. The request the bot uses to load /fish & friends
    try:
        r = requests.get(f'{API}/guilds/{cfg["guild_id"]}/application-command-index', timeout=10,
                         proxies=proxies, headers=headers)
        if r.status_code == 200:
            n = len(r.json().get('application_commands', []))
            say('Load commands', n > 0, f'HTTP 200, {n} commands' + ('' if n else ' -> EMPTY, bot would say "Unknown command: fish"'))
        else:
            say('Load commands', False, f'HTTP {r.status_code} {short(r)}  (this is why "Unknown command: fish" appears)')
    except Exception as e:
        say('Load commands', False, f'{type(e).__name__}: {e}')

    #5. Gateway: connect + identify and report what Discord answers
    ws = WebSocket()
    try:
        kw = {}
        if cfg['proxy_ip'] and cfg['proxy_port']:
            kw = dict(http_proxy_host=cfg['proxy_ip'], http_proxy_port=cfg['proxy_port'])
            if cfg['proxy_user'] and cfg['proxy_pass']:
                kw['http_proxy_auth'] = (cfg['proxy_user'], cfg['proxy_pass'])
        ws.connect(GATEWAY, timeout=15, **kw)
        hello = json.loads(ws.recv())
        say('Gateway connect', hello.get('op') == 10, f'HELLO received (heartbeat {hello["d"]["heartbeat_interval"] / 1000:.1f}s)')
        ws.send(json.dumps({'op': 2, 'd': {'token': cfg['token'], 'properties': {
            'os': 'iOS', 'browser': 'Discord iOS', 'device': 'iPhone 15', 'release_channel': 'stable',
            'client_version': '217.0', 'os_version': '17.4', 'system_locale': 'en-US',
            'client_build_number': 55497}}}))
        deadline = time.time() + 12
        ws.settimeout(12)
        verdict = None
        while time.time() < deadline and verdict is None:
            opcode, data = ws.recv_data(control_frame=True)
            if opcode == 8:
                code = int.from_bytes(data[:2], 'big') if len(data) >= 2 else 1006
                verdict = (False, f'Discord CLOSED the connection after identify: code {code} = {CLOSE_CODES.get(code, "unknown")}')
            elif opcode == 1:
                ev = json.loads(data)
                if ev.get('t') == 'READY':
                    verdict = (True, 'READY received, gateway login works')
                elif ev.get('op') == 9:
                    verdict = (False, 'INVALID SESSION right after identify (token rejected / account flagged)')
        say('Gateway identify', *(verdict or (False, 'no READY within 12s')))
    except WebSocketConnectionClosedException:
        say('Gateway identify', False, 'connection closed by the server without a close code (usually an IP/network block)')
    except Exception as e:
        say('Gateway', False, f'{type(e).__name__}: {e}')
    finally:
        try:
            ws.close()
        except Exception:
            pass

    print('\nSend me this whole output (the token is not included).', flush=True)


if __name__ == '__main__':
    main()
