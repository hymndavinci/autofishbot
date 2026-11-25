#------------------------ IMPORTS --------------------------#
from __future__ import annotations

from . import *
from .utils import debugger
from .menu import NotificationPriority
from time import time, sleep
from urllib.parse import quote
from websocket import WebSocket
from requests import Session, exceptions
from random import choice, randint, random

from string import digits, ascii_letters
from json import loads, dumps, JSONDecodeError
from threading import Event, Lock, Thread


#------------------------ CONSTANTS --------------------------#
OPCODES = [
{'code': 0, 'name': 'Dispatch', 'action': 'Receive', 'description': 'An event was dispatched.'},
{'code': 1, 'name': 'Heartbeat', 'action': 'Send/Receive', 'description': 'Fired periodically by the client to keep the connection alive.'},
{'code': 2, 'name': 'Identify', 'action': 'Send', 'description': 'Starts a new session during the initial handshake.'},
{'code': 3, 'name': 'Presence Update', 'action': 'Send', 'description': 'Update the client\'s presence.'},
{'code': 4, 'name': 'Voice State Update', 'action': 'Send', 'description': 'Used to join/leave or move between voice channels.'},
{'code': 6, 'name': 'Resume', 'action': 'Send', 'description': 'Resume a previous session that was disconnected.'},
{'code': 7, 'name': 'Reconnect', 'action': 'Receive', 'description': 'You should attempt to reconnect and resume immediately.'},
{'code': 8, 'name': 'Request Guild Members', 'action': 'Send', 'description': 'Request information about offline guild members in a large guild.'},
{'code': 9, 'name': 'Invalid Session', 'action': 'Receive', 'description': 'The session has been invalidated. You should reconnect and identify/resume accordingly.'},
{'code': 10, 'name': 'Hello', 'action': 'Receive', 'description': 'Sent immediately after connecting, contains the heartbeat_interval to use.'},
{'code': 11, 'name': 'Heartbeat ACK', 'action': 'Receive', 'description': 'Sent in response to receiving a heartbeat to acknowledge that it has been received.'},
]

GATEWAY_CLOSE = [
{'code': 4000, 'name': 'Unknown error', 'description': 'We\'re not sure what went wrong. Try reconnecting?', 'reconnect': True},
{'code': 4001, 'name': 'Unknown opcode', 'description': 'You sent an invalid Gateway opcode or an invalid payload for an opcode.', 'reconnect': True},
{'code': 4002, 'name': 'Decode error', 'description': 'You sent an invalid payload to us.', 'reconnect': True},
{'code': 4003, 'name': 'Not authenticated', 'description': 'You sent us a payload prior to identifying.', 'reconnect': True},
{'code': 4004, 'name': 'Authentication failed', 'description': 'The account token sent with your identify payload is incorrect.', 'reconnect': False},
{'code': 4005, 'name': 'Already authenticated', 'description': 'You sent more than one identify payload.', 'reconnect': True},
{'code': 4007, 'name': 'Invalid seq', 'description': 'The sequence sent when resuming the session was invalid. Reconnect and start a new session.', 'reconnect': True},
{'code': 4008, 'name': 'Rate limited', 'description': 'Woah nelly! You\'re sending payloads to us too quickly. Slow it down! You will be disconnected on receiving this.', 'reconnect': True},
{'code': 4009, 'name': 'Session timed out', 'description': 'Your session timed out. Reconnect and start a new one.', 'reconnect': True},
{'code': 4010, 'name': 'Invalid shard', 'description': 'You sent us an invalid shard when identifying.', 'reconnect': False},
{'code': 4011, 'name': 'Sharding required', 'description': 'The session would have handled too many guilds - you are required to shard your connection in order to connect.', 'reconnect': False},
{'code': 4012, 'name': 'Invalid API version', 'description': 'You sent an invalid version for the gateway.', 'reconnect': False},
{'code': 4013, 'name': 'Invalid intent(s)', 'description': 'You sent an invalid intent for a Gateway Intent. You may have incorrectly calculated the bitwise value.', 'reconnect': False},
{'code': 4014, 'name': 'Disallowed intent(s)', 'description': 'You sent a disallowed intent for a Gateway Intent. You may have tried to specify an intent that you have not enabled or are not approved for.', 'reconnect': False},
]


class GatewayClosed(Exception):
    '''Raised when Discord closes the gateway connection, keeps the close code.'''
    def __init__(self, code: int, reason: str = '') -> None:
        self.code = code
        self.reason = reason
        super().__init__(self.describe())

    @property
    def info(self) -> dict:
        for item in GATEWAY_CLOSE:
            if item['code'] == self.code:
                return item
        return {}

    @property
    def fatal(self) -> bool:
        '''True when retrying can never work (bad token, invalid intents...).'''
        return bool(self.info) and not self.info.get('reconnect', True)

    def describe(self) -> str:
        name = self.info.get('name') or ('Abnormal close' if self.code == 1006 else 'Unknown')
        text = f'gateway closed: {self.code} {name}'
        if self.code == 4004:
            text += ' (token invalid/expired)'
        elif self.code == 4008:
            text += ' (rate limited, too many connects)'
        if self.reason:
            text += f' - {self.reason[:80]}'
        return text


HTTP_CODES = [
{'code': 200, 'name': 'OK', 'description': 'The request completed successfully.'},
{'code': 201, 'name': 'CREATED', 'description': 'The entity was created successfully.'},
{'code': 204, 'name': 'NO CONTENT', 'description': 'The request completed successfully but returned no content.'},
{'code': 304, 'name': 'NOT MODIFIED', 'description': 'The entity was not modified (no action was taken).'},
{'code': 400, 'name': 'BAD REQUEST', 'description': 'The request was improperly formatted, or the server couldn\'t understand it.'},
{'code': 401, 'name': 'UNAUTHORIZED', 'description': 'The Authorization header was missing or invalid.'},
{'code': 403, 'name': 'FORBIDDEN', 'description': 'The Authorization token you passed did not have permission to the resource.'},
{'code': 404, 'name': 'NOT FOUND', 'description': 'The resource at the location specified doesn\'t exist.'},
{'code': 405, 'name': 'METHOD NOT ALLOWED', 'description': 'The HTTP method used is not valid for the location specified.'},
{'code': 429, 'name': 'TOO MANY REQUESTS', 'description': 'You are being rate limited, see Rate Limits.'},
{'code': 502, 'name': 'GATEWAY UNAVAILABLE', 'description': 'There was not a gateway available to process your request. Wait a bit and retry.'},
{'code': '5xx', 'name': 'SERVER ERROR', 'description': 'The server had an error processing your request (these are rare).'},
]

DEFAULT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
NON_CRITICAL_CODES = (400, 429, 502, 503, 504)
TARGET_EVENT_NAMES = ('MESSAGE_CREATE', 'MESSAGE_UPDATE')
INVALID_FORM_BODY = 50035
APPLICATION_ID = '574652751745777665'
COMMAND = 'slash-command-interaction'
BUTTON = 'button-interaction'


def _safe_json(content: bytes) -> dict:
    try:
        parsed = loads(content)
        return parsed if isinstance(parsed, dict) else {'raw': parsed}
    except (JSONDecodeError, UnicodeDecodeError, TypeError):
        return {'raw': (content or b'').decode('utf-8', 'replace')[:200]}


#------------------------- CLASSES ---------------------------#
@dataclass(slots=True)
class Proxy:
    '''Proxy class, responsible for validating and constructing proper - ready-to-use - arguments
    for HTTP and WebSocket connections.'''
    ip: str | None = None
    port: str | None = None
    user: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    validator_urls: list = field(default_factory=list, repr=False)
    _max_timeout: int = field(default=10, repr=False)
    is_valid: bool = False

    def __post_init__(self) -> None:
        self.validator_urls = ['https://ident.me', 'https://api.my-ip.io/ip']

    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'

    def setup(self, ip: str | None, port: str | None, user: str | None = None, password: str | None = None) -> None:
        '''Assigns the class variables and calls the validate method.'''
        if ip and port:
            self.ip = ip
            self.port = port
            self.user = user
            self.password = password

            if self.validate():
                return None
            #Invalid proxy, using real ip.
            self.ip = None
            self.port = None
            self.user = None
            self.password = None

    def proxies(self) -> dict | None:
        '''Returns a dictionary containing the proxies argument for the 'requests' package.
        Proxy credentials (when set) are embedded in the proxy URL so they do not override
        the destination Authorization header.'''
        if self.ip and self.port:
            if self.user and self.password:
                u = quote(str(self.user), safe='')
                p = quote(str(self.password), safe='')
                proxy_url = f'http://{u}:{p}@{self.ip}:{self.port}'
            else:
                proxy_url = f'http://{self.ip}:{self.port}'
            return {
                'http': proxy_url,
                'https': proxy_url
            }
        return None

    def auth(self) -> tuple | None:
        '''Returns a tuple containing the proxy auth tuple for websocket-client.'''
        if self.user and self.password:
            return (self.user, self.password)
        return None

    def validate(self) -> bool:
        '''Validates the proxy and returns True if valid (uses real ip if validation failed).'''
        with Session() as client:
            for url in self.validator_urls:
                print(f'[*] Validating proxy at {url}...', flush=True)
                try:
                    request = client.get(url, proxies=self.proxies(), timeout=self._max_timeout)
                    if request.status_code == 200:
                        print(f'[*] Using proxy: {request.content.decode("utf-8", "replace").strip()}', flush=True)
                        self.is_valid = True
                        return True
                except exceptions.ProxyError as e:
                    print(f'[!] Proxy validation Failed -> {e}', flush=True)
                except exceptions.ReadTimeout:
                    print(f'[!] Attempt timed out ({self._max_timeout}s). Consider using a faster proxy.', flush=True)
                except KeyboardInterrupt:
                    exit('\n[!] User exited during proxy validation.')
                except Exception as e:
                    debugger.log(e, f'{self.name} - validate')
                    print(f'[E] Proxy validation unknown error -> {e}', flush=True)
        print('\n\n[WARNING] PROXY VALIDATION FAILED, USING REAL IP.\n\n', flush=True)
        return False


@dataclass(frozen=True, slots=True)
class ApiEndpoints:
    channel_id: str = field(repr=False)
    guild_id: str = field(repr=False)
    version: int = 10
    interactions: str = ''
    application_commands: str = ''
    gateway: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(self, 'interactions', f'https://discord.com/api/v{self.version}/interactions')
        object.__setattr__(self, 'application_commands', f'https://discord.com/api/v{self.version}/guilds/{self.guild_id}/application-command-index')
        object.__setattr__(self, 'gateway', f'wss://gateway.discord.gg/?v={self.version}&encoding=json')


@dataclass(slots=True)
class DiscordWrapper:
    '''Discord API wrapper, responsible for connecting to the gateway, handling its events,
    and sending interactions over Discord HTTP API.'''

    #Pointers
    config: ConfigManager = field(repr=False)
    menu: BaseMenu = field(repr=False)

    #User (token excluded from repr to prevent leaks into debug.log)
    user_token: str = field(init=False, repr=False)
    channel_id: str = field(init=False)
    guild_id: str = field(init=False)

    #Discord
    session_id: str = field(init=False, repr=False)
    commands: list[dict] = field(repr=False, default_factory=list)
    seq: int | None = None
    user_id: str = '' #Own account id (taken from the READY event), used to ignore other players' messages
    username: str = '' #Own account username (taken from the READY event), used as the webhook label
    fatal_close: str = '' #Set when Discord refused the login for good (e.g. 4004 / HTTP 401), nothing to retry
    endpoints: ApiEndpoints = field(init=False, repr=False)

    device: str = field(init=False)
    #Network
    ws: WebSocket = field(init=False, repr=False)
    _http: Session = field(init=False, repr=False)
    _ws_send_lock: Lock = field(default_factory=Lock, repr=False)
    _hb_stop: Event = field(default_factory=Event, repr=False)
    user_agent: str = field(default=DEFAULT_USER_AGENT, repr=False)
    proxy: Proxy = field(default_factory=Proxy, repr=False)
    headers: dict = field(default_factory=dict, repr=False)
    _request_timeout: int = 15
    _jitter: float = field(default_factory=random)
    _default_heartbeat_interval: int = 41250
    _beating: bool = False
    _hb_gen: int = 0 #Heartbeat generation: only the newest heartbeat thread is allowed to live
    _recv_timeout: float = 120.0 #Gateway sends a heartbeat ACK every ~41s, so >120s of silence = dead socket
    _rate_limit_safe_guard: int = 3

    #Flags
    is_connected: bool = False
    is_ready: bool = False
    is_reconnecting: bool = False
    auto_connect: bool = False

    def __post_init__(self) -> None:
        '''Loads values from config and initializes HTTP session / gateway.'''
        #User
        self.user_token = self.config.user_token
        self.channel_id = self.config.channel_id
        self.guild_id = self.config.guild_id
        self.set_random_device()

        #Network
        self.ws = WebSocket()
        if self.config.user_agent:
            self.user_agent = self.config.user_agent

        self.headers = {
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json',
            'Authorization': self.user_token
        }

        self.proxy.setup(
            ip=self.config.proxy_ip,
            port=self.config.proxy_port,
            user=self.config.proxy_auth_user,
            password=self.config.proxy_auth_password
        )

        self._http = Session()
        self._http.headers.update(self.headers)
        proxies = self.proxy.proxies()
        if proxies:
            self._http.proxies.update(proxies)

        #Discord
        self.session_id = self.make_session()
        self.endpoints = ApiEndpoints(self.channel_id, self.guild_id)

        if self.auto_connect:
            if self.guild_id:
                self.commands = self.load_commands()
            self.connect()
            if not self.commands and self.guild_id:
                self.endpoints = ApiEndpoints(self.channel_id, self.guild_id)
                self.commands = self.load_commands()

    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'

    @property
    def heartbeat(self) -> dict:
        '''Returns the gateway ping event.'''
        return {
            'op': 1,
            'd': self.seq
        }

    @property
    def snowflake(self) -> int:
        '''Generates Discord's snowflake data type.'''
        inc = self.seq if self.seq else randint(0, 10**3)
        return int(time() * 1000 - 1420070400000) * (1024 * 4096) + inc

    def set_random_device(self) -> None:
        device_values = ['iPhone 15 Pro', 'iPhone 15', 'iPhone 14', 'iPhone 13']
        self.device = choice(device_values)

    @property
    def passport(self) -> dict:
        '''Returns the gateway identifier payload.'''
        return {
            'op': 2,
            'd': {
                'token': self.user_token,
                'properties': {
                    'os': 'iOS',
                    'browser': 'Discord iOS',
                    'release_channel': 'stable',
                    'device': self.device,
                    'client_version': '217.0',
                    'os_version': '17.4',
                    'os_arch': 'x64',
                    'app_arch': 'ia32',
                    'system_locale': 'en-US',
                    'referrer': '',
                    'referring_domain': '',
                    'referrer_current': '',
                    'referring_domain_current': '',
                    'client_build_number': 55497,
                },
                'nonce': self.snowflake
            }
        }

    def make_session(self, session: str = '') -> str:
        '''Generates user session.'''
        return session.join(choice(ascii_letters + digits) for _ in range(32))

    def _ws_send(self, payload: dict) -> None:
        encoded = dumps(payload)
        with self._ws_send_lock:
            self.ws.send(encoded)

    def make_data(self, message_id: str | None, custom_id: str | None, category: str,
                  command: str | None, parameters: dict | None = None) -> dict | None:
        '''Builds data object containing a request for Discord's interactions API.'''
        if category == COMMAND:
            if not command:
                return None
            cmd_data = None

            #Validate command
            for cmd in self.commands:
                try:
                    if command.lower() == cmd['name'].lower():
                        cmd_data = cmd
                        break
                except KeyError:
                    print('[!] Failed to parse command data.', flush=True)
                    return None

            if cmd_data:
                #Use the option name Discord actually defines (e.g. /verify <result> vs <answer>)
                try:
                    option_names = [o['name'] for o in (cmd_data.get('options') or [])]
                    if parameters and option_names and parameters.get('name') not in option_names:
                        parameters = {**parameters, 'name': option_names[0]}
                except (KeyError, TypeError, AttributeError):
                    pass
                return {
                    'type': 2,
                    'application_id': APPLICATION_ID,
                    'guild_id': self.guild_id,
                    'channel_id': self.channel_id,
                    'session_id': self.session_id,
                    'data': {
                        'version': cmd_data['version'],
                        'id': cmd_data['id'],
                        'name': cmd_data['name'],
                        'type': int(cmd_data['type']),
                        'options': [parameters] if parameters else [],
                        'application_command': cmd_data,
                        'attachments': []
                    },
                    'nonce': self.snowflake
                }
            self.menu.notify(f'[!] Unknown command: {command}', NotificationPriority.NORMAL)
            return None
        elif category == BUTTON:
            return {
                'type': 3,
                'nonce': self.snowflake,
                'guild_id': self.guild_id,
                'channel_id': self.channel_id,
                'message_flags': 0,
                'message_id': message_id,
                'application_id': APPLICATION_ID,
                'data': {
                    'component_type': 2,
                    'custom_id': custom_id
                },
                'session_id': self.session_id
            }
        else:
            self.menu.notify(f'[!] Invalid category: {category}', NotificationPriority.LOW)
            return None

    def request(self, message_id: str | None = None, custom_id: str | None = None, category: str | None = None,
                command: str | None = None, parameters: dict | None = None, method: str = 'post',
                endpoint: str | None = None, get_data: any = None):
        '''Sends a Discord HTTP API request with error and rate-limit handling.'''
        if not endpoint:
            endpoint = self.endpoints.interactions
        if not category:
            category = COMMAND

        data = None
        try:
            match method:
                case 'post':
                    data = self.make_data(message_id, custom_id, category, command, parameters)
                    if not data:
                        return None
                    response_obj = self._http.post(
                        url=endpoint, data=dumps(data), verify=True, timeout=self._request_timeout
                    )
                case 'get':
                    response_obj = self._http.get(
                        url=endpoint, data=get_data, verify=True, timeout=self._request_timeout
                    )
                case _:
                    return False
        except exceptions.ReadTimeout:
            self.menu.notify(f'[!] Request timed out ({self._request_timeout}s).')
            return False
        except exceptions.ProxyError:
            self.menu.notify('[!] Proxy error.')
            return False
        except exceptions.ConnectionError as e:
            debugger.log(e, f'{self.name} - request connection error')
            self.menu.notify('[!] Request connection error.')
            return False
        except Exception as e:
            debugger.log(e, f'{self.name} - request')
            self.menu.notify(f'[!] Request - Unknown error: {e}')
            return False

        status_code = response_obj.status_code
        headers = response_obj.headers

        if status_code == 200:
            #Successful requests that need to return data
            return _safe_json(response_obj.content)
        elif status_code > 204:
            #Request error
            if status_code in NON_CRITICAL_CODES:
                #Bad request
                if status_code == 400:
                    response = _safe_json(response_obj.content)
                    err_message = f'Code: {status_code}\nResponse: {response}\nRequest: {data}\nHeaders: {headers}'

                    if category == BUTTON or int(response.get('code', 0)) == INVALID_FORM_BODY:
                        #Non critical, fall back to /fish
                        debugger.log(err_message, 'ax0')
                        self.menu.notify('[*] Button rejected by Discord, falling back to /fish.', NotificationPriority.LOW)
                        return False
                    debugger.log(err_message, 'ax1')
                    exit(f'{err_message}\nax1')

                #Rate limits
                elif status_code == 429:
                    response = _safe_json(response_obj.content)
                    try:
                        if not response.get('global', False):
                            message = str(response.get('message', 'Rate limited'))
                            waiting_time = self._rate_limit_safe_guard + float(response['retry_after'])
                            self.menu.notify(
                                f'[429] {message.capitalize()} waiting {waiting_time:.1f} seconds.',
                                NotificationPriority.HIGH
                            )
                            sleep(waiting_time)
                            return True
                        else:
                            message = str(response.get('message', 'Global rate limit'))
                            self.fatal_close = f'HTTP 429 (Global): {message}'
                            exit(f'[E] 429 (Global): {message}')
                    except (KeyError, TypeError, ValueError) as e:
                        debugger.log(f'{e} | body={response}', f'{self.name} 429')
                        #Cloudflare/HTML 429 without retry_after: wait safely instead of crashing
                        self.menu.notify('[429] Rate limited (no retry_after), waiting 30s.', NotificationPriority.HIGH)
                        sleep(30)
                        return False
                else:
                    #502/503/504 Gateway unavailable
                    self.menu.notify(f'[!] Gateway unavailable: {status_code}. Waiting 30 seconds.')
                    sleep(30)
                    return False
            else:
                if category == BUTTON and status_code in (403, 404):
                    #Button not ours / message gone: not fatal, caller falls back to /fish
                    debugger.log(f'{status_code} on button interaction', f'{self.name} button')
                    self.menu.notify(f'[*] Button rejected ({status_code}), falling back to /fish.', NotificationPriority.LOW)
                    return False
                #Critical, debug and exit
                action_label = command or (f'GET {endpoint}' if method == 'get' else category)
                debugger.log(f'status={status_code} action={action_label} data={data}', f'{self.name} {status_code}')
                if status_code == 401:
                    self.fatal_close = 'HTTP 401 Unauthorized (token invalid/expired)'
                elif status_code == 403:
                    self.fatal_close = 'HTTP 403 Forbidden (no access to channel/guild or account restricted)'
                exit(f'[E] Discord returned HTTP {status_code} ({action_label}). Stopping this account.')
        else:
            #201 and 204 - Successful interactions
            return True

    def load_guild_id(self) -> str:
        '''Checks the READY event and returns the guild id (if found).'''
        while not self.guild_id:
            event = self.receive_event()
            if event and event.get('t') == 'READY':
                for guild in event['d'].get('guilds', []):
                    for channel in guild.get('channels', []):
                        if str(channel.get('id')) == self.channel_id:
                            return str(guild['id'])
                self.fatal_close = 'Incorrect channel_id (not found in READY guilds)'
                exit('[E] Incorrect channel id.')

    def load_commands(self) -> list:
        '''Searches for the application commands from the configured guild.'''
        try:
            content = self.request(endpoint=self.endpoints.application_commands, method='get')
            if not isinstance(content, dict):
                return []
            return [obj for obj in content.get('application_commands', []) if obj.get('application_id') == APPLICATION_ID]
        except SystemExit:
            raise
        except Exception as e:
            debugger.log(e, f'{self.name} - load_commands')
            return []

    #----------------------------------GATEWAY----------------------------------#
    def receive_event(self) -> dict | None:
        '''Receives events from the gateway and handles protocol control opcodes.'''
        opcode, data = self.ws.recv_data()
        if opcode == 8:
            #Close frame: keep the code/reason instead of silently dropping it
            code = int.from_bytes(data[:2], 'big') if len(data) >= 2 else 1006
            reason = data[2:].decode('utf-8', 'replace') if len(data) > 2 else ''
            raise GatewayClosed(code, reason)
        if opcode == 1:
            event = data.decode('utf-8')
        elif opcode == 2:
            event = data
        else:
            event = None

        if not event:
            return None

        deserialized = loads(event)
        seq = deserialized.get('s')
        if seq is not None:
            self.seq = seq

        op = deserialized.get('op')
        if op == 1:
            #Discord requested an immediate heartbeat
            try:
                self._ws_send(self.heartbeat)
            except Exception as e:
                debugger.log(e, f'{self.name} - immediate heartbeat')
            return deserialized
        elif op == 7:
            raise GatewayClosed(1012, 'Discord requested reconnect')
        elif op == 9:
            raise GatewayClosed(4009, 'Invalid session')

        if deserialized.get('t') == 'READY':
            try:
                self.user_id = str(deserialized['d']['user']['id'])
                self.username = str(deserialized['d']['user']['username'])
                notifier = getattr(self.menu, 'notifier', None)
                if notifier is not None and self.username:
                    notifier.label = self.username
            except (KeyError, TypeError):
                pass
        return deserialized

    def connect(self) -> bool:
        '''Sets up gateway connection: connects, reads HELLO synchronously, starts heartbeat thread,
        and sends identify event.'''
        try:
            if not self.is_reconnecting:
                print('[*] Connecting to gateway ...', flush=True)
            self.ws.connect(
                self.endpoints.gateway,
                timeout=15,
                http_proxy_host=self.proxy.ip,
                http_proxy_port=self.proxy.port,
                http_proxy_auth=self.proxy.auth()
            )
            self.is_connected = True

            #Read HELLO synchronously before starting heartbeat / Receiver threads to avoid concurrent recv()
            try:
                self.ws.settimeout(15)
                hello = self.receive_event()
                if hello and hello.get('op') == 10 and isinstance(hello.get('d'), dict):
                    interval = int(hello['d']['heartbeat_interval']) / 1000.0
                else:
                    interval = self._default_heartbeat_interval / 1000.0
            except GatewayClosed:
                raise
            except Exception as e:
                debugger.log(e, f'{self.name} - connect hello')
                interval = self._default_heartbeat_interval / 1000.0

            #Without a timeout recv() blocks forever on a half-dead connection (the bot looks "stuck").
            #With it, the Receiver gets an exception and goes through its normal reconnect flow.
            try:
                self.ws.settimeout(self._recv_timeout)
            except Exception as e:
                debugger.log(e, f'{self.name} - connect settimeout')

            #Start heartbeat thread
            if not self.is_reconnecting:
                print('[*] Starting heartbeat thread ...', flush=True)
            self._hb_stop.clear()
            self._hb_gen += 1
            Thread(
                target=self.start_heartbeat,
                args=(interval, True, self._hb_gen),
                daemon=True,
                name='Heartbeat'
            ).start()

            #Send identify event
            if not self.is_reconnecting:
                print('[*] Logging in ...', flush=True)
            self._ws_send(self.passport)

            #Load the guild id if not configured
            if not self.guild_id:
                self.guild_id = self.load_guild_id()

            self.is_ready = True
            if not self.is_reconnecting:
                print('[*] Session established!', flush=True)
                try:
                    notifier = getattr(self.menu, 'notifier', None)
                    if notifier is not None and self.username:
                        notifier.label = self.username
                except Exception:
                    pass
                if self.username:
                    self.menu.notify(f'[*] Connected to Discord as {self.username}.')
                else:
                    self.menu.notify('[*] Connected to Discord.')
            return True
        except GatewayClosed as e:
            self.is_connected = False
            self.is_ready = False
            if e.fatal:
                self.fatal_close = e.describe()
            debugger.log(f'{e} (channel={self.channel_id}, guild={self.guild_id})', f'{self.name} - connect')
            raise
        except Exception as e:
            self.is_connected = False
            self.is_ready = False
            debugger.log(f'{type(e).__name__}: {e} (channel={self.channel_id}, guild={self.guild_id})', f'{self.name} - connect')
            raise RuntimeError(f'Gateway connection failed: {type(e).__name__}: {e}') from e

    def reconnect(self) -> bool:
        '''Safely recreate a gateway connection; caller controls retry/backoff.'''
        self.is_reconnecting = True
        try:
            self._hb_stop.set()
            if self.is_connected:
                #A remotely closed socket can make shutdown() raise; still proceed with a fresh socket.
                try:
                    self.ws.shutdown()
                except Exception as close_error:
                    debugger.log(close_error, f'{self.name} - reconnect shutdown')
                self.is_connected = False
                self.is_ready = False

            #Give the previous heartbeat worker a bounded time to stop.
            wait_started = time()
            while self._beating and time() - wait_started < 5:
                sleep(0.1)
            if self._beating:
                raise RuntimeError('Previous heartbeat worker did not stop.')

            self.ws = WebSocket()
            self.session_id = self.make_session()
            self.seq = None
            return bool(self.connect())
        except Exception as e:
            self.is_connected = False
            self.is_ready = False
            debugger.log(e, f'{self.name} - reconnect')
            return False
        finally:
            self.is_reconnecting = False

    def disconnect(self) -> bool:
        '''Disconnects completely from the gateway and closes the HTTP session.'''
        self.is_connected = False
        self.is_ready = False
        self._hb_stop.set()
        try:
            self._http.close()
        except Exception:
            pass
        try:
            self.ws.shutdown()
            if not self.is_reconnecting:
                print('[*] Disconnected!', flush=True)
            return True
        except Exception as e:
            debugger.log(e, f'{self.name} - disconnect')
            return False

    def start_heartbeat(self, interval: float | None = None, startup: bool = True, gen: int | None = None) -> None:
        '''Sends periodic heartbeats to the gateway.
        `gen` identifies this thread; when a newer connection starts, older threads exit immediately.'''
        if gen is None:
            gen = self._hb_gen
        if interval is None or interval <= 0:
            interval = self._default_heartbeat_interval / 1000.0

        def alive() -> bool:
            return self.is_connected and gen == self._hb_gen and not self._hb_stop.is_set()

        self._beating = True
        try:
            while alive():
                wait_for = (interval * self._jitter) if startup else interval
                startup = False
                #Event.wait wakes immediately when reconnect/disconnect sets _hb_stop
                if self._hb_stop.wait(timeout=max(0.5, wait_for)):
                    break
                if not alive():
                    break
                try:
                    self._ws_send(self.heartbeat)
                except Exception as e:
                    if alive():
                        self.menu.notify(f'[E] Failed to send heartbeat: {e}', NotificationPriority.HIGH)
                        debugger.log(e, f'{self.name} - start_heartbeat')
                    break
        finally:
            if gen == self._hb_gen:
                self._beating = False
        return None


# --------- INIT ---------#
if __name__ == '__main__':
    pass
