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
    'device_role':'single', 'handoff_dir':'', 'handoff_required':False,
    'handoff_transport':'folder', 'handoff_web_url':'',
}


def load_settings(path=None):
    path = Path(path) if path else data_directory()/'automation.json'
    value = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    return validate_settings(value)


def validate_settings(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):
        raise ValueError('Unsupported automation settings')
    result = {**DEFAULTS,**value}
    for name in ('auto_send','allow_partial_report','enabled','collection_enabled','weekly_archive_enabled','hiworks_enabled','handoff_required'):
        if type(result[name]) is not bool:
            raise ValueError('Settings switches must be boolean')
    for name in ('folder','mailbox','model','hiworks_company','report_template','handoff_dir','device_role','handoff_transport','handoff_web_url'):
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
    if result['device_role'] not in {'single','omen','company-relay'}:
        raise ValueError('Unsupported device role')
    if result['handoff_transport'] not in {'folder','web'}:
        raise ValueError('Unsupported handoff transport')
    if result['handoff_web_url']:
        from .cloud_handoff import validate_url
        result['handoff_web_url']=validate_url(result['handoff_web_url'])
    if result['handoff_transport']=='web' and (result['device_role'] not in {'company-relay','omen'} or not result['handoff_web_url']):
        raise ValueError('Company web delivery requires the approved company folder URL')
    if result['device_role']=='company-relay' and (result['mailbox'] or result['teams_chats'] or result['hiworks_enabled'] or result['enabled'] or result['auto_send']):
        raise ValueError('Company relay may only collect the shared folder')
    if result['device_role']=='omen' and result['weekly_archive_enabled']:
        raise ValueError('Omen imports the company weekly archive; it must not recreate it')
    if result['device_role']=='omen' and not result['handoff_required']:
        raise ValueError('Omen requires the company Friday archive before scheduled reports')
    if type(result['max_input_chars']) is not int or not 1000<=result['max_input_chars']<=1000000:
        raise ValueError('AI input limit is invalid')
    if type(result['collection_interval_minutes']) is not int or not 5<=result['collection_interval_minutes']<=1440:
        raise ValueError('Collection interval must be 5 to 1440 minutes')
    return result
