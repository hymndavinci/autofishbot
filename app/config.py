#------------------------ IMPORTS --------------------------#
from __future__ import annotations

from . import *
from configparser import ConfigParser
from os import chmod, environ, getcwd, listdir, makedirs, replace, stat
from os.path import exists, join
from webbrowser import Error as wbError, open as wbOpen
from datetime import date
from random import randbytes
from re import sub
import sys
from sys import argv


#------------------------ CONSTANTS --------------------------#
REPO_CONFIG = 'https://github.com/hymndavinci/autofishbot/blob/main/assets/template.config'


def _is_non_interactive() -> bool:
    '''True when running in headless / Pterodactyl / container without interactive TTY.'''
    flag = environ.get('AFB_HEADLESS', '').strip().lower()
    if flag in ('1', 'true', 'on', 'yes'):
        return True
    if flag in ('0', 'false', 'off', 'no'):
        return False
    return bool(environ.get('P_SERVER_UUID')) or not sys.stdin.isatty()


#------------------------- CLASSES ---------------------------#
class GenericException(Exception):
    pass


class MissingRequiredFieldError(Exception):
    def __init__(self, field_name: str) -> None:
        super().__init__(f"The field '{field_name}' is required.")


class OutsideBoundariesError(Exception):
    def __init__(self, limits: tuple, value: any, field: str = 'Unk') -> None:
        super().__init__(f"{field}: Value outside boundaries ({limits[0]}, {limits[1]}), '{value}' given.")


@dataclass(slots=True)
class ConfigManager:
    '''The configuration file manager for autofishbot.'''

    #System (sensitive values excluded from repr to prevent leaks in logs)
    user_token: str = field(default='', repr=False)
    user_cooldown: float = 3.5
    channel_id: str = ''
    guild_id: str = ''
    debug: bool = False
    config_path: str = 'configs'
    config_file: str = '' #When set, this file is loaded directly (no selection dialog) - used by multi.py

    #Captcha
    ocr_api_key: str = field(default='', repr=False)

    #Network
    user_agent: str = ''
    proxy_ip: str = ''
    proxy_port: str = ''
    proxy_auth_user: str = field(default='', repr=False)
    proxy_auth_password: str = field(default='', repr=False)

    #Automation
    boosts_length: int = 5
    more_fish: bool = False
    more_treasures: bool = False
    fish_on_exit: bool = False
    auto_daily: bool = False
    auto_buy_baits: bool = False
    auto_sell: bool = False
    auto_update_inventory: bool = False

    #Menu
    compact_mode: bool = False
    refresh_rate: float = 0.3

    #Cosmetic
    pet: str = ''
    bait: str = ''
    biome: str = ''

    #External notifications (optional)
    discord_webhook_url: str = field(default='', repr=False)
    telegram_bot_token: str = field(default='', repr=False)
    telegram_chat_id: str = ''

    #Web dashboard (optional, ideal for Pterodactyl allocated port)
    web_enabled: bool = False
    web_host: str = '0.0.0.0'
    web_port: int = 0
    web_password: str = field(default='', repr=False)

    _selected_config: str = field(default='', init=False, repr=False)
    _path: str = field(init=False, repr=False)
    _configs: list[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._path = join(getcwd(), self.config_path)
        makedirs(self._path, exist_ok=True)

        if self.config_file:
            self._configs = [self.config_file]
            self.load_config(self.config_file)
            return None

        self._configs = self.list_configs()

        if len(argv) >= 2 and argv[1] == '--create':
            self.create_config()
            sys.exit(0)

        #Optional env var for Pterodactyl / Docker startup configuration
        env_cfg = environ.get('AFB_CONFIG', '').strip()
        if env_cfg:
            matched = self._resolve_config_name(env_cfg)
            if matched:
                self.load_config(matched)
                return None
            print(f'[E] AFB_CONFIG="{env_cfg}" not found in {self._path}. Available: {self._configs}', flush=True)
            sys.exit(0 if _is_non_interactive() else 1)

        if self._configs:
            if len(self._configs) > 1:
                selected = self.choice_dialog()
                self.load_config(selected)
            else:
                self.load_config(self._configs[0])
        else:
            self.create_config()
            sys.exit(0)

    def _resolve_config_name(self, raw_name: str) -> str | None:
        target = raw_name.strip().lower()
        for name in self._configs:
            if name.lower() == target or name.lower() == f'{target}.config':
                return name
        return None

    def load_config(self, selected_config: str) -> None:
        '''Loads a config file.'''
        print(f'[*] Loading "{selected_config}" ...', flush=True)
        self._selected_config = selected_config

        target = join(self._path, selected_config)
        cfg = ConfigParser(interpolation=None)
        if not cfg.read(target, encoding='utf-8'):
            print(f'[E] Config file not found or unreadable: "{target}".', flush=True)
            sys.exit(0 if _is_non_interactive() else 1)

        try:
            system = cfg['SYSTEM']
            captcha = cfg['CAPTCHA']
            network = cfg['NETWORK']
            automation = cfg['AUTOMATION']
            menu = cfg['MENU']
            cosmetics = cfg['COSMETIC']
        except KeyError as e:
            self.err_dialog(
                f'[E] Outdated or incomplete config file (missing section {e}).\n'
                f'TIP: Run "python autofishbot.py --create" or copy assets/template.config.'
            )
            sys.exit(0 if _is_non_interactive() else 1)

        try:
            #System
            self.user_token = self.to_str(system['user_token'], field='USER_TOKEN')
            self.user_cooldown = self.to_float(system['user_cooldown'], field='USER_COOLDOWN')
            self.channel_id = self.to_str(system['channel_id'], field='CHANNEL_ID')
            self.guild_id = self.to_str(system['guild_id'], field='GUILD_ID')
            self.debug = self.to_bool(system['debug'])

            #Captcha
            self.ocr_api_key = self.to_str(captcha['ocr_api_key'], field='OCR_API_KEY')

            #Network
            self.user_agent = self.to_str(network['user_agent'], required=False) or ''
            self.proxy_ip = self.to_str(network['proxy_ip'], required=False) or ''
            self.proxy_port = self.to_str(network['proxy_port'], required=False) or ''
            self.proxy_auth_user = self.to_str(network['proxy_auth_user'], required=False) or ''
            self.proxy_auth_password = self.to_str(network['proxy_auth_password'], required=False) or ''

            #Automation
            self.boosts_length = self.to_int(automation['boosts_length'], field='BOOSTS_LENGTH')
            self.more_fish = self.to_bool(automation['more_fish'])
            self.more_treasures = self.to_bool(automation['more_treasures'])
            self.fish_on_exit = self.to_bool(automation['fish_on_exit'])
            self.auto_daily = self.to_bool(automation['auto_daily'])
            self.auto_buy_baits = self.to_bool(automation['auto_buy_baits'])
            self.auto_sell = self.to_bool(automation['auto_sell'])
            self.auto_update_inventory = self.to_bool(automation['auto_update_inventory'])

            #Menu
            self.compact_mode = self.to_bool(menu['compact_mode'])
            self.refresh_rate = self.to_float(menu['refresh_rate'], field='REFRESH_RATE', bd=(0.1, 1))

            #Cosmetic
            self.pet = self.to_str(cosmetics['pet'], False) or ''
            self.bait = self.to_str(cosmetics['bait'], False) or ''
            self.biome = self.to_str(cosmetics['biome'], False) or ''

            notifications = cfg['NOTIFICATIONS'] if cfg.has_section('NOTIFICATIONS') else {}
            self.discord_webhook_url = str(notifications.get('discord_webhook_url', '')).strip()
            self.telegram_bot_token = str(notifications.get('telegram_bot_token', '')).strip()
            self.telegram_chat_id = str(notifications.get('telegram_chat_id', '')).strip()
            if not self.discord_webhook_url:
                #Backwards compat: older configs kept the webhook URL under [MENU]
                self.discord_webhook_url = str(menu.get('discord_webhook_url', '')).strip()

            web = cfg['WEB'] if cfg.has_section('WEB') else {}
            self.web_enabled = self.to_bool(web.get('enabled', False)) if web else False
            self.web_host = (self.to_str(web.get('host', ''), required=False) if web else None) or '0.0.0.0'
            raw_port = str(web.get('port', '')).strip() if web else ''
            if raw_port:
                try:
                    self.web_port = int(raw_port)
                except ValueError:
                    self.web_port = 0
            else:
                self.web_port = 0
            self.web_password = str(web.get('password', '')).strip() if web else ''
        except KeyError as e:
            self.err_dialog(f'[E] Err -> {e} key missing | Outdated config file.')
            sys.exit(0 if _is_non_interactive() else 1)
        except (MissingRequiredFieldError,
                OutsideBoundariesError,
                GenericException,
                ValueError) as e:
            print(f'[E] Err -> {e}', flush=True)
            sys.exit(0 if _is_non_interactive() else 1)

    def create_config(self) -> None:
        '''Creates a new config file.'''
        print('[*] Generating new config...', flush=True)

        cfg = ConfigParser(interpolation=None)
        cfg['SYSTEM'] = {
            'user_token': self.user_token,
            'user_cooldown': self.user_cooldown,
            'channel_id': self.channel_id,
            'guild_id': self.guild_id,
            'debug': self.debug
        }
        cfg['CAPTCHA'] = {
            'ocr_api_key': self.ocr_api_key
        }
        cfg['NETWORK'] = {
            'user_agent': self.user_agent,
            'proxy_ip': self.proxy_ip,
            'proxy_port': self.proxy_port,
            'proxy_auth_user': self.proxy_auth_user,
            'proxy_auth_password': self.proxy_auth_password
        }
        cfg['AUTOMATION'] = {
            'boosts_length': self.boosts_length,
            'more_fish': self.more_fish,
            'more_treasures': self.more_treasures,
            'fish_on_exit': self.fish_on_exit,
            'auto_daily': self.auto_daily,
            'auto_buy_baits': self.auto_buy_baits,
            'auto_sell': self.auto_sell,
            'auto_update_inventory': self.auto_update_inventory
        }
        cfg['MENU'] = {
            'compact_mode': self.compact_mode,
            'refresh_rate': self.refresh_rate
        }
        cfg['COSMETIC'] = {
            'pet': self.pet,
            'bait': self.bait,
            'biome': self.biome
        }
        cfg['NOTIFICATIONS'] = {
            'discord_webhook_url': self.discord_webhook_url,
            'telegram_bot_token': self.telegram_bot_token,
            'telegram_chat_id': self.telegram_chat_id
        }
        cfg['WEB'] = {
            'enabled': self.web_enabled,
            'host': self.web_host,
            'port': self.web_port or '',
            'password': self.web_password
        }

        non_interactive = _is_non_interactive()
        if non_interactive:
            cfg_name = 'default'
        else:
            try:
                cfg_name = input('[?] Please insert a name for this config: ')
                if not cfg_name or len(sub('[a-zA-Z0-9 _-]', '', cfg_name)) > 0:
                    print('[!] Invalid name, a generated name will be used instead.')
                    cfg_name = self.make_name()
            except (KeyboardInterrupt, EOFError):
                exit('\n[!] User exited.')

        exit_message = ''
        try:
            if not cfg_name.endswith('.config'):
                cfg_name += '.config'
            target = join(self._path, cfg_name)
            with open(target, 'w', encoding='utf-8') as f:
                cfg.write(f)
            exit_message = (
                f'\n[!] Config "{cfg_name}" successfully created at "{self._path}".\n'
                f'[!] Please fill in your USER_TOKEN, CHANNEL_ID, GUILD_ID, and OCR_API_KEY, then start again.'
            )
            if not non_interactive:
                wbOpen(target)
        except wbError as e:
            exit_message = f'\n[E] Unable to open default text editor, please edit it manually. Details: {e}'
        except Exception as e:
            exit_message = f'\n[E] Unable to create new config -> {e}\n[!] NOTE: You can download a sample config file at: {REPO_CONFIG}'
        finally:
            if exit_message:
                print(exit_message, flush=True)

    def save_automation(self) -> None:
        '''Persist current automation toggles without replacing unrelated config values.'''
        if not self._selected_config:
            raise RuntimeError('No active config file is selected.')
        target = join(self._path, self._selected_config)
        cfg = ConfigParser(interpolation=None)
        if not cfg.read(target, encoding='utf-8'):
            raise FileNotFoundError(target)
        if not cfg.has_section('AUTOMATION'):
            cfg.add_section('AUTOMATION')
        values = {
            'more_fish': self.more_fish,
            'more_treasures': self.more_treasures,
            'fish_on_exit': self.fish_on_exit,
            'auto_daily': self.auto_daily,
            'auto_buy_baits': self.auto_buy_baits,
            'auto_sell': self.auto_sell,
            'auto_update_inventory': self.auto_update_inventory,
        }
        for key, value in values.items():
            cfg.set('AUTOMATION', key, str(bool(value)))
        temporary = target + '.tmp'
        try:
            with open(temporary, 'w', encoding='utf-8') as stream:
                cfg.write(stream)
            try:
                chmod(temporary, stat(target).st_mode & 0o777)
            except OSError:
                pass
            replace(temporary, target)
        except Exception:
            try:
                from os import remove
                if exists(temporary):
                    remove(temporary)
            except Exception:
                pass
            raise

    def list_configs(self, arr: list | None = None) -> list[str]:
        '''List all *.config files from the config directory (sorted).'''
        arr = [] if arr is None else arr
        if not exists(self._path):
            return arr
        for file in sorted(listdir(self._path)):
            if file.endswith('.config'):
                arr.append(file)
        return arr

    def to_str(self, value: any, required: bool = True, field: str | None = None) -> str | None:
        '''Validate string values.'''
        value = sub('["\']', '', str(value or '')).strip()
        if value:
            return value
        if required:
            raise MissingRequiredFieldError(field or 'UNKNOWN')
        return None

    def to_bool(self, value: any) -> bool:
        '''Converts and evaluates string values to boolean.'''
        if value:
            return str(value).strip().lower() in ('1', 'true', 'on', 'yes')
        return False

    def to_int(self, value: any, field: str | None = None) -> int:
        '''Converts boosts_length config value to supported integer tier (5 or 20).'''
        if value:
            try:
                if int(value) > 12.5:
                    return 20
            except ValueError as e:
                raise GenericException(f'{field}: {e}')
        return 5

    def to_float(self, value: any, field: str | None = None, bd: tuple = (2, 3.5)) -> float:
        '''Converts and evaluates string values to float within boundaries `bd`.'''
        if value:
            try:
                numeric = float(value)
            except ValueError as e:
                raise GenericException(f'{field}: {e}.')

            if self.compare(numeric, bd):
                return numeric
            raise OutsideBoundariesError(bd, numeric, field or 'Unk')
        if field == 'REFRESH_RATE':
            return 0.3
        raise MissingRequiredFieldError(field or 'UNKNOWN')

    def compare(self, value: float, bd: tuple) -> bool:
        '''Compares if value is between boundaries.'''
        l_min, l_max = bd
        return l_min <= value <= l_max

    def err_dialog(self, message: str) -> None:
        '''Error dialog to optionally create a new config (safe in headless mode).'''
        print(f'{message}', flush=True)
        if _is_non_interactive():
            return None
        try:
            if input('[?] Do you want to create a new config ? (y/n) ').lower() == 'y':
                self.create_config()
                sys.exit(0)
            else:
                exit('[!] User exited.')
        except (KeyboardInterrupt, EOFError):
            exit('\n[!] User exited.')
        except Exception as e:
            exit(f'\n[!] User exited ({e}).')

    def choice_dialog(self) -> str:
        '''Choosing dialog to select one config (non-blocking & safe on Pterodactyl).'''
        if len(argv) >= 2:
            try:
                index = int(argv[1])
                if 0 < index <= len(self._configs):
                    return self._configs[index - 1]
                print(f'Invalid choice. {index} is outside the scope.', flush=True)
            except ValueError:
                matched = self._resolve_config_name(argv[1])
                if matched:
                    return matched
                print(f'Invalid choice. "{argv[1]}" is outside the scope.', flush=True)

        if _is_non_interactive():
            #On Pterodactyl/headless without an explicit config argument: avoid hanging on input()
            #and avoid EOFError infinite loop. Give clear instructions and exit 0 (no restart loop).
            print(f'[!] Multiple configs detected in {self._path}: {", ".join(self._configs)}', flush=True)
            print('[!] Set environment variable AFB_CONFIG=<filename.config> (or pass it as an argument), '
                  'or set PY_FILE=multi.py to run all configs.', flush=True)
            sys.exit(0)

        print(f'[!] Multiple configs detected in {self._path} folder.\nPlease choose:')
        for k, config in enumerate(self._configs):
            print(f'\t{k + 1} - "{config}"')
        while True:
            try:
                index = int(input('[?] Insert the config number: '))
                if 0 < index <= len(self._configs):
                    return self._configs[index - 1]
                raise GenericException(f'Invalid choice. {index} is outside the scope.')
            except (KeyboardInterrupt, EOFError):
                exit('\n[!] User exited.')
            except Exception as e:
                print(f'[E] Try again. Err -> {e}')

    def make_name(self) -> str:
        return f'{date.today().strftime("%d-%m-%y")}-{randbytes(5).hex()}'


# --------- INIT ---------#
if __name__ == '__main__':
    config = ConfigManager()
    print(config)
