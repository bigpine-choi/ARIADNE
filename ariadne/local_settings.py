"""Local settings contain paths/account scope, never API keys."""
import json
from pathlib import Path
from .config import data_directory


DEFAULTS = {
    'folder': '', 'mailbox': '', 'teams_chats': [],
    'model': '', 'auto_send': False, 'allow_partial_report': False,
    'enabled': False, 'max_input_chars': 240000,
    'collection_enabled': False, 'collection_interval_minutes': 60,
    'weekly_archive_enabled': False, 'hiworks_company':'', 'hiworks_enabled':False,
    'report_template':'',
}


def load_settings(path=None):
    path = Path(path) if path else data_directory()/'automation.json'
    value = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    return validate_settings(value)


def validate_settings(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):
        raise ValueError('Unsupported automation settings')
    result = {**DEFAULTS,**value}
    for name in ('auto_send','allow_partial_report','enabled','collection_enabled','weekly_archive_enabled','hiworks_enabled'):
        if type(result[name]) is not bool:
            raise ValueError('Settings switches must be boolean')
    for name in ('folder','mailbox','model','hiworks_company','report_template'):
        if not isinstance(result[name],str) or len(result[name]) > 2000:
            raise ValueError('Settings text is invalid')
        result[name] = result[name].strip()
    if result['mailbox'] and ('@' not in result['mailbox'] or any(c.isspace() for c in result['mailbox'])):
        raise ValueError('Mailbox must be an email address')
    result['mailbox'] = result['mailbox'].casefold()
    if result['hiworks_company']:
        import re
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,100}', result['hiworks_company']):
            raise ValueError('Hiworks company domain is invalid')
    if not isinstance(result['teams_chats'],list) or any(not isinstance(c,str) or not c.strip() for c in result['teams_chats']):
        raise ValueError('Teams chats must contain exact window chat titles')
    if len(result['teams_chats']) > 20 or any(len(c) > 300 for c in result['teams_chats']):
        raise ValueError('Teams chat scope is too large')
    result['teams_chats'] = list(dict.fromkeys(c.strip() for c in result['teams_chats']))
    if type(result['max_input_chars']) is not int or not 1000<=result['max_input_chars']<=1000000:
        raise ValueError('AI input limit is invalid')
    if type(result['collection_interval_minutes']) is not int or not 5<=result['collection_interval_minutes']<=1440:
        raise ValueError('Collection interval must be 5 to 1440 minutes')
    return result
