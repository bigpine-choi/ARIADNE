"""Scoped Teams UI Automation collector. Navigation/scroll only, no message input."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .reporting import KST


def normalize(rows, chats, start, end):
    messages=[]; unknown=0; seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!= {'id','text','chat'} or row['chat'] not in chats:
            raise ValueError('Teams response escaped configured chat scope')
        if not re.fullmatch(r'message-body-\d+',row['id']) or not isinstance(row['text'],str) or len(row['text'])>100000:
            raise ValueError('Invalid Teams message')
        match=re.search(r'(\d{4})년 (\d{1,2})월 (\d{1,2})일 .+? (오전|오후) (\d{1,2}):(\d{2})',row['text'])
        stamp=None
        if match:
            y,m,d,ampm,h,minute=match.groups()
            hour=int(h)%12+(12 if ampm=='오후' else 0)
            try: stamp=datetime(int(y),int(m),int(d),hour,int(minute),tzinfo=KST)
            except ValueError: pass
        if stamp and not start<=stamp<end: continue
        if stamp is None: unknown+=1
        identifier=row['chat']+':'+row['id']
        if identifier in seen: continue
        seen.add(identifier)
        messages.append({'source_id':identifier,'provider':'teams_uia','chat':row['chat'],
            'subject':row['chat'],'body':row['text'],'timestamp':stamp.isoformat() if stamp else None,
            'sender':'','direction':'unknown','timestamp_precision':'minute','captured_at':datetime.now(timezone.utc).isoformat()})
    return messages,unknown


def collect(chats, start, end):
    if os.name!='nt': raise RuntimeError('Teams UI Automation requires Windows')
    if not chats: return {'messages':[],'complete':False,'warnings':['teams_scope_not_configured']}
    if start.tzinfo is None or end.tzinfo is None or start>=end: raise ValueError('Aware collection interval required')
    with tempfile.TemporaryDirectory(prefix='ariadne-teams-') as directory:
        config=Path(directory)/'scope.json'; result=Path(directory)/'result.json'
        config.write_text(json.dumps({'chats':chats,'start':start.isoformat()},ensure_ascii=False),encoding='utf-8-sig')
        command=[str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'),
            '-NoProfile','-NonInteractive','-STA','-File',str(Path(__file__).parent/'windows/teams.ps1'),
            '-SettingsPath',str(config),'-ResultPath',str(result)]
        completed=subprocess.run(command,capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if completed.returncode or not result.exists(): raise RuntimeError('Teams accessibility collection unavailable')
        raw=json.loads(result.read_text(encoding='utf-8-sig'))
    messages,unknown=normalize(raw['messages'],chats,start,end)
    warnings=list(raw['warnings'])
    if unknown: warnings.append('teams_message_dates_unverified')
    # UI virtualization/attachments are never claimed to be complete Graph history.
    warnings.append('teams_screen_scope_only')
    return {'messages':messages,'complete':False,'warnings':sorted(set(warnings)),'coverage':raw['coverage']}
