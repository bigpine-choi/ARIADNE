"""Local collection commands and scheduler entrypoint."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .automation_store import AutomationStore, canonical
from .config import data_directory
from .local_settings import load_settings
from . import local_mail, local_teams
from .weekly_worker import run_weekly, scheduled_tick


def main():
    parser=argparse.ArgumentParser(description='ARIADNE local collection and weekly reports')
    parser.add_argument('--settings',type=Path)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('command',choices=['doctor','sync','history','weekly','tick','collect-mail','collect-teams'])
    arguments=parser.parse_args()
    if arguments.data_dir:
        import os
        os.environ['ARIADNE_DATA_DIR']=str(arguments.data_dir.resolve())
    settings=load_settings(arguments.settings)
    if arguments.command=='doctor':
        import os
        result={'folderAccessible':Path(settings['folder']).is_dir() if settings['folder'] else False,
          'mailboxConfigured':bool(settings['mailbox']),
          'apiKeyConfigured':bool(os.environ.get('OPENAI_API_KEY')),
          'modelConfigured':bool(settings['model']),'teamsMode':'local_keyboard_copy_experimental',
          'enabled':settings['enabled'],'autoSend':settings['auto_send']}
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
                teams=local_teams.collect(settings['teams_chats'])
                count=store.ingest_messages('teams:'+settings['mailbox'].casefold(),teams['messages'])
                result={'read':len(teams['messages']),'stored':count,'complete':False,'warnings':teams['warnings']}
            else:
                result=(run_weekly if arguments.command=='weekly' else scheduled_tick)(store,settings)
        finally:
            store.close()
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':
    main()
