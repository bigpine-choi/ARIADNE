"""Local collection commands and scheduler entrypoint."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .automation_store import AutomationStore, canonical
from .config import data_directory
from .local_settings import load_settings
from . import local_mail, teams_uia
from .weekly_worker import run_weekly, scheduled_tick


def main():
    parser=argparse.ArgumentParser(description='ARIADNE local collection and weekly reports')
    parser.add_argument('--settings',type=Path)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('command',choices=['doctor','sync','history','weekly','tick','collect-mail','collect-teams','archive-weekly'])
    arguments=parser.parse_args()
    if arguments.data_dir:
        import os
        os.environ['ARIADNE_DATA_DIR']=str(arguments.data_dir.resolve())
    settings=load_settings(arguments.settings)
    role=settings.get('device_role','single')
    if role=='company-relay' and arguments.command in {'collect-mail','collect-teams','weekly'}:
        raise ValueError('회사 PC는 공유폴더 보관·전달만 실행합니다.')
    if role=='omen' and arguments.command in {'sync','archive-weekly'}:
        raise ValueError('오멘에서는 웹 화면의 회의 자료 받기를 사용하세요.')
    if arguments.command=='doctor':
        from .credentials import CredentialStore
        key_status = CredentialStore().status()
        result={'folderAccessible':Path(settings['folder']).is_dir() if settings['folder'] else False,
          'mailboxConfigured':bool(settings['mailbox']),
          'apiKeyConfigured':key_status['saved'] or key_status['environmentConfigured'],
          'apiConnectionVerified':False,
          'modelConfigured':bool(settings['model']),'teamsMode':'ui_automation_no_input',
          'teamsSchedule':'weekdays_1200_KST_once_no_catchup',
          'enabled':settings['enabled'],'autoSend':settings['auto_send']}
        from .native_powershell import diagnostics
        result['mailDiagnostics']=diagnostics(local_mail.SCRIPT,require_outlook=True)
        result['teamsDiagnostics']=diagnostics(Path(teams_uia.__file__).parent/'windows/teams.ps1')
    else:
        store=AutomationStore(data_directory()/'automation.sqlite3')
        try:
            if arguments.command=='sync':
                if not settings['folder']:
                    raise ValueError('Configure a folder first')
                result=store.sync_folder(settings['folder'])
            elif arguments.command=='history':
                result=store.history('folder:'+canonical(settings['folder']))
            elif arguments.command=='collect-mail':
                from datetime import timedelta
                end=datetime.now(timezone.utc)
                mail=local_mail.collect(settings['mailbox'],end-timedelta(days=7),end)
                count=store.ingest_messages('outlook:'+settings['mailbox'].casefold(),mail['messages'])
                result={'read':len(mail['messages']),'stored':count,'complete':mail['complete'],
                        'warnings':mail['warnings']}
            elif arguments.command=='collect-teams':
                from datetime import timedelta
                end=datetime.now(timezone.utc)
                teams=teams_uia.collect(settings['teams_chats'],end-timedelta(days=7),end)
                count=store.ingest_messages('teams:'+settings['mailbox'].casefold(),teams['messages'])
                result={'read':len(teams['messages']),'stored':count,'complete':False,'warnings':teams['warnings']}
            elif arguments.command=='archive-weekly':
                from .weekly_archive import archive_weekly
                result=archive_weekly(store,settings,force=True)
            else:
                from .workspace import Workspace
                from .browser_bridge import BrowserBridge
                from .calendar import CalendarStore
                from .repository import Repository
                repository=Repository(data_directory()/'ariadne.sqlite3')
                bridge=BrowserBridge(store,data_directory()) if role!='company-relay' else None
                if bridge:bridge.start()
                workspace=Workspace(store,arguments.settings or data_directory()/'automation.json',repository,
                    calendar_store=CalendarStore(data_directory()/'calendar.sqlite3'),browser_bridge=bridge)
                try:
                    if arguments.command=='tick':
                        job=workspace.start('tick')
                        workspace.worker.join()
                        result=next(row for row in workspace.jobs() if row['id']==job['id'])
                    else:result=workspace.prepare_weekly(settings)
                finally:
                    workspace.close()
                    if bridge:bridge.close()
                    repository.close()

        finally:
            store.close()
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':
    main()
