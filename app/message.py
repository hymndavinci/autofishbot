#------------------------ IMPORTS --------------------------#
from __future__ import annotations

from . import *
from .utils import sanitize
from re import sub


#------------------------- CLASSES ---------------------------#

@dataclass(slots=True, frozen=True)
class MessageCategory:
    '''Essential keyworks to categorize each message title.'''
    fish: str = 'You caught'
    profile: str = 'Inventory of'
    charms: str = 'Charms are found in'
    buffs: str = 'current multipliers'
    quests: str = 'Quest List'
    leaderboard: str = '\'s leaderboard positions'


@dataclass
class Message:
    '''Message class to convert raw events into usable messages.'''
    id: str = None
    play_id: str = None
    title: str = field(default=None, repr=False)
    description: str = field(default=None, repr=False)
    content: str = None
    items: list[str] = field(default_factory=list, repr=False)
    untitled: str = None
      
    @property
    def name(self) -> str:
        '''Returns the class name in the correct format.'''
        return f'{self.__class__.__name__}'

    @property
    def text(self) -> str:
        '''All visible text of the message, classic embeds and Components V2 alike.'''
        return ' '.join(filter(None, [self.title, self.description, self.content, self.untitled]))
    
    @staticmethod
    def _walk_components(components: list) -> tuple[list, list]:
        '''Walks (possibly nested) components, returning (buttons, texts).
        Supports classic action rows and Components V2 containers / text displays.'''
        buttons, texts = [], []
        for c in components or []:
            if not isinstance(c, dict):
                continue
            if c.get('type') == 2:  #Button
                buttons.append(c)
            if c.get('type') == 10 and c.get('content'):  #Text display (components v2)
                texts.append(c['content'])
            if c.get('components'):
                b, t = Message._walk_components(c['components'])
                buttons += b
                texts += t
            if isinstance(c.get('accessory'), dict) and c['accessory'].get('type') == 2:
                buttons.append(c['accessory'])
        return buttons, texts

    def make(self, event: dict) -> None:
        '''Makes message schematics, assigning ids and attributes.'''
        self.reset()
        if not event:
            return None

        self.content = event.get('content', '') or ''

        #Assign ids (looks for the "Fish Again" button anywhere in the components tree)
        buttons, texts = self._walk_components(event.get('components', []))
        custom_id = None
        for b in buttons:
            label = (b.get('label') or '').lower()
            if 'fish again' in label and b.get('custom_id'):
                custom_id = b['custom_id']
        if custom_id:
            self.id = event.get('id')
            self.play_id = custom_id

        #Assign attributes (classic embeds)
        embeds = event.get('embeds', []) or []
        for embed in embeds:
            if 'title' in embed:
                self.title = embed['title']
                self.description = embed.get('description')
                if self.title and 'You caught' in self.title:
                    break
            else:
                #Untitled
                if 'description' in embed:
                    self.untitled = sanitize(embed['description'])
                else:
                    self.untitled = sanitize(self.content)

        #Fallback: Components V2 (no embeds, content lives in text displays)
        if not self.title and texts:
            joined = '\n'.join(texts)
            for i, t in enumerate(texts):
                if 'You caught' in t:
                    first, _, rest = joined.partition('\n')
                    self.title = 'You caught'
                    self.description = joined.split('You caught', 1)[1].lstrip(': \n')
                    break
            else:
                self.untitled = sanitize(joined)
        return None

    def build(self, extra_list: list = []) -> list:
        '''Builds message list ready to be displayed.'''
        #Todo: refactor this function
        for line in (self.description or '').split('\n'):
            final = ''
            if line.find('LEVEL UP!') > -1:
                #Level up information
                level = sub('[^\d+]', '', sub(r'<.+?> ', '', line))
                final = f'<< LEVEL UP: {int(level) - 1} -> {level} >>'
            elif line.find('<:') > -1:
                #Emotes 
                final = sub(r'  ', ' ', sub(r'<.+?>', '', line))
            elif line.find(':') > -1:
                #Duplicator Emote (and commom emotes)
                final = sub(r'  ', ' ', sub(r':.+?: ', '', line))
            elif line.find('#') > -1: 
                #Global boost information
                pass
            elif line == '' or line == None or line == '\n':
                pass
            else:
                final = line
            self.items.append(sub('[\*+]', '', final))
        if extra_list != []:
            self.items = extra_list + self.items
        return self.items
    
    def reset(self) -> None:
        '''Reset values to default.'''
        self.title: str = None
        self.description: str = None
        self.content: str = None
        self.items: list[str] = []
        self.untitled: str = None
        return None
    
    def reset_ids(self) -> None:
        self.play_id = None
        self.id = None

