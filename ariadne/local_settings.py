"""Local settings contain paths/account scope, never API keys."""
import json
from pathlib import Path
from .config import data_directory


DEFAULTS = {
    'folder': '', 'mailbox': '', 'teams_chats': [],
    'model': '', 'auto_send': False, 'allow_partial_report': False,
    'enabled': False, 'max_input_chars': 240000,
}


def load_settings(path=None):
    path = Path(path) if path else data_directory()/'automation.json'
    value = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):
        raise ValueError('Unsupported automation settings')
    result = {**DEFAULTS,**value}
    for name in ('auto_send','allow_partial_report','enabled'):
        if type(result[name]) is not bool:
            raise ValueError('Settings switches must be boolean')
    for name in ('folder','mailbox','model'):
        if not isinstance(result[name],str):
            raise ValueError('Settings text is invalid')
    if not isinstance(result['teams_chats'],list) or any(not isinstance(c,str) or not c.strip() for c in result['teams_chats']):
        raise ValueError('Teams chats must contain exact window chat titles')
    if type(result['max_input_chars']) is not int or not 1000<=result['max_input_chars']<=1000000:
        raise ValueError('AI input limit is invalid')
    return result
