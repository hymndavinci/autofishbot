#------------------------ IMPORTS --------------------------#
from __future__ import annotations

from . import *
from .utils import debugger
from .menu import NotificationPriority
from requests import post, exceptions
from threading import Thread
from time import sleep
from json import loads
from re import sub

#------------------------ CONSTANTS --------------------------#
MAX_CAPTCHA_REGENS = 5  #Virtual Fisher allows /verify regen up to 5 times

#------------------------- CLASSES ---------------------------#
class UnkownCaptchaError(Exception):
    pass

#Todo: make a captcha status (like scheduler class)


@dataclass
class Captcha:
    '''Catpcha class, detects and solves captchas.'''
    #Todo: split this class in chaptcha "controller" and captcha datatype
    #Pointers
    menu: BaseMenu = field(repr=False)
    
    #Components
    api_key: str = field(repr=False)
    answers: list = field(default_factory=list)
    captcha_image: str = None
    
    #Backend
    _ocr_url: str = field(default='https://api.ocr.space/parse/image', repr=False)
    _word_list: list[str] = field(init=False, repr=False)
    _raw_answers: list[str] = field(default_factory=list)
    _engines: list[int] = field(init=False, repr=False)
    _max_timeout: int = field(default=20, repr=False)
    _captcha_min_length: int = 4  #codes seen in the wild: 4 chars (older ones had 6)
    _captcha_max_length: int = 6
    
    #Counters
    regens: int = 0
    
    #Flags
    busy: bool = False
    detected: bool = False
    solving: bool = False
    regenerating: bool = False

    #OCR Settings
    is_overlay_required: bool = field(default=False, repr=False)
    detect_orientation: bool = field(default=True, repr=False)
    scale: bool = field(default=False, repr=False)
    language: str = field(default='eng', repr=False)
    
    def __post_init__(self) -> None:
        self._word_list = ['captcha', 'verify']
        self._engines = [2, 1, 3, 5]
    
    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'
    
    def filter(self, value: str) -> str:
        '''Filters results form the API. Valid results -> only alphanum and 4 <= len <= 6 (case is kept)'''
        if value:
            ans = sub('[^a-zA-Z0-9]', '', value)
            if self._captcha_min_length <= len(ans) <= self._captcha_max_length:
                return ans
            else: 
                #Invalid result
                return None
        else:
            return None
    
    def request(self, engine: int) -> None:
        #Todo: make this function less complex and better structured
        '''Makes a request to the OCR api and appends the result (if valid) to the answers list.'''
        if not self.detected: 
            return None

        payload = {
            'apikey': self.api_key,
            'url': self.captcha_image,
            'isOverlayRequired': self.is_overlay_required,
            'detectOrientation': self.detect_orientation,
            'scale': self.scale,
            'OCREngine': engine,
            'language': self.language
        }
        
        try:
            request = post(self._ocr_url, data=payload, timeout=self._max_timeout)
            response = loads(request.content.decode())
        except exceptions.ReadTimeout as e:
            #Took too long to respond
            self.menu.notify(f'[!] Engine {engine} took too long to respond.', NotificationPriority.LOW)
            if engine == self._engines[-1]:
                self.solving = False
            debugger.log(e, f'{self.name} - request timeout | {self}')
            return None
        except Exception as e:
            debugger.log(e, f'{self.name} - request')
            raise UnkownCaptchaError(e)
        
        if response['OCRExitCode'] == 1:
            if self.detected:
                answer = self.filter(response['ParsedResults'][0]['ParsedText'])
                if answer:
                    #To avoid conflicts due to multi-threading, it must not be duplicate, 
                    #it must not be solved and must be busy (to garatee that the object it's the same)
                    if answer not in self.answers and self.detected:
                        self.answers.append(answer)
                    else:
                        #Duplicate result
                        pass
            
            if engine == self._engines[-1]:
                if not self.detected: 
                    return None
                self.solving = False
            return None
        else:
            #API error
            if not self.detected: 
                return None
            self.menu.notify(
                f'[!] Engine {engine} OCR API error -> ExitCode: {response["OCRExitCode"]}.', 
                NotificationPriority.HIGH
            )
            if engine == self._engines[-1]:
                self.solving = False
            return None     
            
    #------------------------ event helpers --------------------------#
    @staticmethod
    def _walk_components(components: list, texts: list, images: list) -> None:
        '''Collects text and media urls from (nested) components - classic rows and Components V2.'''
        for c in components or []:
            if not isinstance(c, dict):
                continue
            if isinstance(c.get('content'), str):
                texts.append(c['content'])
            if isinstance(c.get('label'), str):
                texts.append(c['label'])
            media = c.get('media')
            if isinstance(media, dict) and media.get('url'):
                images.append(media['url'])
            for item in c.get('items', []) or []:  #media gallery
                if isinstance(item, dict) and isinstance(item.get('media'), dict) and item['media'].get('url'):
                    images.append(item['media']['url'])
            if isinstance(c.get('file'), dict) and c['file'].get('url'):
                images.append(c['file']['url'])
            if isinstance(c.get('accessory'), dict):
                Captcha._walk_components([c['accessory']], texts, images)
            if c.get('components'):
                Captcha._walk_components(c['components'], texts, images)

    def _inspect(self, event: dict) -> tuple[str, str | None]:
        '''Returns (visible_text_lowercase, captcha_image_url or None) for an event.'''
        texts, images = [], []
        if isinstance(event.get('content'), str):
            texts.append(event['content'])

        for embed in event.get('embeds', []) or []:
            for key in ('title', 'description'):
                if isinstance(embed.get(key), str):
                    texts.append(embed[key])
            for key in ('footer', 'author'):
                if isinstance(embed.get(key), dict):
                    texts.append(str(embed[key].get('text') or embed[key].get('name') or ''))
            for fld in embed.get('fields', []) or []:
                texts.append(f"{fld.get('name', '')} {fld.get('value', '')}")
            for key in ('image', 'thumbnail'):
                if isinstance(embed.get(key), dict) and embed[key].get('url'):
                    images.append(embed[key]['url'])

        self._walk_components(event.get('components', []), texts, images)

        for att in event.get('attachments', []) or []:
            if isinstance(att, dict) and att.get('url') and str(att.get('content_type', 'image')).startswith('image'):
                images.append(att['url'])

        return ('\n'.join(texts).lower(), images[0] if images else None)

    def detect(self, event: dict) -> bool:
        '''Attempts to detect any captchas in the event. Never raises: a captcha without a
        readable image is reported as detected (image = None) and handled by regen/manual input.'''
        self.busy = True
        text, image = self._inspect(event)
        has_keyword = any(word in text for word in self._word_list)

        if has_keyword:
            self.detected = True

        if self.detected and (has_keyword or image):
            if image:
                self.captcha_image = image
            else:
                self.captcha_image = None
                debugger.log('Captcha detected but no image found in event', f'{self.name} - detect')
            self.busy = False
            return True
        elif self.detected:
            #Already in a captcha flow but this event is unrelated
            self.busy = False
            return False
        else:
            self.busy = False
            self.reset()
            return False

    def solve(self) -> None:
        '''Start engine threads to solve the captcha.'''
        #Todo: try asyncio instead of threads
        self.busy = True
        self.regenerating = False
        self.answers = [] #Reset answers for redundancy

        if not self.captcha_image:
            #Nothing for the OCR to read: let the dispatcher regenerate / wait for manual input
            self.menu.notify('[!] Captcha detected but no image found.', NotificationPriority.HIGH)
            self.solving = False
            self.busy = False
            return None

        self.solving = True
        
        for engine in self._engines:
            async_request = Thread(target=self.request, args=(engine,), daemon=True)
            async_request.start()
            sleep(0.5)
        
        self.busy = False
        return None
    
    def reset(self) -> None:
        '''Reset all attrs to default values.'''
        self.answers = []
        self.busy = False
        self.detected = False
        self.regenerating = False
        self.regens = 0
        return None
        
        
# --------- INIT ---------#
if __name__ == "__main__":
    pass