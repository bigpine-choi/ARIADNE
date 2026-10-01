"""Unified collection -> durable evidence -> calendar/report workflows."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from uuid import uuid4

from . import local_mail, teams_uia
from .automation_store import now, canonical
from .local_settings import load_settings, validate_settings
from .models import ValidationError
from .reporting import KST, report_window
from .weekly_worker import run_weekly
from .weekly_archive import archive_weekly, archive_runs


def collection_slot(at):
    if at.tzinfo is None: raise ValueError('Aware scheduler time required')
    local=at.astimezone(KST)
    if local.weekday()>=5 or not 9<=local.hour<=18 or local.minute>=5: return None
    return local.replace(minute=0,second=0,microsecond=0).isoformat()


def teams_slot(at):
    """A missed noon run must not unexpectedly navigate Teams later in the day."""
    if at.tzinfo is None: raise ValueError('Aware scheduler time required')
    local=at.astimezone(KST)
    if local.weekday()>=5 or local.hour!=12 or local.minute>=5: return None
    return local.date().isoformat()


def process_alive(pid):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()!=87 # Unknown/denied means keep the lock.
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


class Workspace:
    def __init__(self,store,settings_path,repository,*,calendar_store=None,browser_bridge=None,
                 mail=None,teams=None,reporter=None,sender=None):
        self.store,self.settings_path,self.repository=store,Path(settings_path),repository
        self.calendar_store,self.bridge=calendar_store,browser_bridge
        self.mail,self.teams,self.reporter,self.sender=mail or local_mail.collect,teams or teams_uia.collect,reporter,sender
        self.lock,self.stop=threading.RLock(),threading.Event()
        with self.store.lock:
            self.store.connection.executescript("""CREATE TABLE IF NOT EXISTS workspace_jobs(
            id TEXT PRIMARY KEY,action TEXT NOT NULL,status TEXT NOT NULL,started TEXT NOT NULL,
            finished TEXT,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hourly_runs(id TEXT PRIMARY KEY,status TEXT NOT NULL,attempts INTEGER NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS teams_daily_runs(id TEXT PRIMARY KEY,status TEXT NOT NULL,started TEXT NOT NULL,result TEXT NOT NULL);""")
            self.store.connection.execute('CREATE TABLE IF NOT EXISTS workspace_job_owners(id TEXT PRIMARY KEY,pid INTEGER NOT NULL)')
        self.worker=None;self.scheduler=None

    def settings(self):
        with self.lock:return load_settings(self.settings_path)

    def save_settings(self,value):
        try:result=validate_settings(value)
        except ValueError:raise ValidationError('연결 설정의 계정·경로·스위치를 확인하세요.') from None
        with self.lock:
            self.settings_path.parent.mkdir(parents=True,exist_ok=True)
            temp=None
            try:
                with tempfile.NamedTemporaryFile(dir=self.settings_path.parent,delete=False,mode='w',encoding='utf-8') as file:
                    temp=Path(file.name);json.dump(result,file,ensure_ascii=False)
                    file.flush();os.fsync(file.fileno())
                temp.replace(self.settings_path)
            finally:
                if temp:temp.unlink(missing_ok=True)
        return result

    def jobs(self):
        with self.store.lock:
            rows=self.store.connection.execute('SELECT * FROM workspace_jobs ORDER BY started DESC LIMIT 30').fetchall()
        return [{**dict(r),'result':json.loads(r['result'])} for r in rows]

    def start(self,action):
        if action not in {'mail','teams','hiworks','folder','weekly','collect','tick','archive'}:
            raise ValidationError('지원하지 않는 작업입니다.')
        with self.lock:
            settings=self.settings()
            if action in {'mail','teams'} and not settings['mailbox']:raise ValidationError('수집 메일 계정을 저장하세요.')
            if action=='teams' and not settings['teams_chats']:raise ValidationError('Teams 대화 이름을 저장하세요.')
            if action in {'folder','archive'} and not settings['folder']:raise ValidationError('수집 폴더를 저장하세요.')
            identifier=str(uuid4())
            with self.store.transaction() as db:
                stale=(datetime.now(timezone.utc)-timedelta(minutes=25)).isoformat()
                abandoned=db.execute("""SELECT j.id,o.pid FROM workspace_jobs j LEFT JOIN workspace_job_owners o ON o.id=j.id
                  WHERE j.status='running' AND julianday(j.started)<julianday(?)""",(stale,)).fetchall()
                for row in abandoned:
                    if row['pid'] is None or not process_alive(row['pid']):
                        db.execute("UPDATE workspace_jobs SET status='interrupted',finished=? WHERE id=?",(now(),row['id']))
                if db.execute("SELECT 1 FROM workspace_jobs WHERE status='running'").fetchone():
                    raise ValidationError('수집 또는 보고 작업이 진행 중입니다.')
                db.execute("INSERT INTO workspace_jobs VALUES(?,?,'running',?,NULL,'{}')",(identifier,action,now()))
                db.execute('INSERT INTO workspace_job_owners VALUES(?,?)',(identifier,os.getpid()))
            self.worker=threading.Thread(target=self._run,args=(identifier,action,settings),daemon=True)
            self.worker.start()
        return {'id':identifier,'status':'running'}

    def _collect_mail(self,settings,at=None):
        end=at or datetime.now(timezone.utc)
        result=self.mail(settings['mailbox'],end-timedelta(days=7),end)
        count=self.store.ingest_messages('outlook:'+settings['mailbox'],result['messages'])
        return {'read':len(result['messages']),'stored':count,'complete':bool(result.get('complete')),
                'warnings':result.get('warnings',[])}

    def _collect_teams(self,settings,at=None):
        end=at or datetime.now(timezone.utc)
        result=self.teams(settings['teams_chats'],end-timedelta(days=7),end)
        if any(m.get('chat') not in settings['teams_chats'] for m in result['messages']):
            raise ValidationError('Teams 수집 범위가 설정한 대화와 다릅니다.')
        count=self.store.ingest_messages('teams:'+settings['mailbox'],result['messages'])
        return {'status':'deferred' if 'teams_in_use_collection_deferred' in result.get('warnings',[]) else 'partial',
                'read':len(result['messages']),'stored':count,'complete':bool(result.get('complete')),
                'warnings':result.get('warnings',[]),'coverage':result.get('coverage',[])}

    def _collect_hiworks(self,settings):
        if not self.bridge:raise ValidationError('브라우저 수집 연결이 없습니다.')
        return self.bridge.collect(settings['hiworks_company'])

    def collect(self,settings,at=None):
        result={}
        operations=[('folder',lambda:self.store.sync_folder(settings['folder'])),
                    ('mail',lambda:self._collect_mail(settings,at)),
                    ('hiworks',lambda:self._collect_hiworks(settings))]
        configured=[bool(settings['folder']),bool(settings['mailbox']),settings['hiworks_enabled']]
        for (name,operation),enabled in zip(operations,configured):
            if not enabled:result[name]={'status':'not_configured','complete':False};continue
            try:result[name]={'status':'collected',**operation()}
            except Exception as error:
                result[name]={'status':'failed','complete':False,'errorType':type(error).__name__,
                              'message':'연결 또는 수집 범위를 확인하세요.'}
        return result

    def tick(self,at=None):
        at=at or datetime.now(timezone.utc);settings=self.settings();result={}
        slot=collection_slot(at)
        noon=teams_slot(at)
        # Claim before touching Teams: no retry, catch-up or hourly-run dependency.
        if settings['collection_enabled'] and noon and settings['mailbox'] and settings['teams_chats']:
            with self.store.transaction() as db:
                claimed=db.execute("INSERT OR IGNORE INTO teams_daily_runs VALUES(?,'running',?,'{}')",(noon,now())).rowcount==1
            if claimed:
                try:teams=self._collect_teams(settings,at)
                except Exception as error:
                    teams={'status':'failed','complete':False,'errorType':type(error).__name__}
                with self.store.transaction() as db:
                    db.execute('UPDATE teams_daily_runs SET status=?,result=? WHERE id=?',
                               (teams['status'],json.dumps(teams,ensure_ascii=False),noon))
                result['teams']=teams
        if settings['collection_enabled'] and slot:
            with self.store.transaction() as db:
                row=db.execute('SELECT * FROM hourly_runs WHERE id=?',(slot,)).fetchone()
                claim=not row or row['status']=='failed' and row['attempts']<3
                if claim:
                    db.execute("""INSERT INTO hourly_runs VALUES(?,'running',1,'{}')
                    ON CONFLICT(id) DO UPDATE SET status='running',attempts=attempts+1""",(slot,))
            if claim:
                sources=self.collect(settings,at)
                state='failed' if any(s['status']=='failed' for s in sources.values()) else 'completed'
                with self.store.transaction() as db:
                    db.execute('UPDATE hourly_runs SET status=?,result=? WHERE id=?',(state,json.dumps(sources,ensure_ascii=False),slot))
                result['collection']=sources
        _,end=report_window(at)
        if (at-end).total_seconds()<=3*86400:
            if settings['weekly_archive_enabled']:
                try:result['archive']=archive_weekly(self.store,settings,at)
                except Exception as error:result['archive']={'status':'failed','errorType':type(error).__name__}
            # The archive cutoff stays Friday 16:00; generate after the collection slot.
            if settings['enabled'] and at>=end+timedelta(minutes=15):
                try:result['weekly']=self.prepare_weekly(settings,at)
                except Exception as error:result['weekly']={'status':'failed','errorType':type(error).__name__}
        return result or {'status':'not_due'}

    def _run(self,identifier,action,settings):
        try:
            if action=='mail':result=self._collect_mail(settings)
            elif action=='teams':result=self._collect_teams(settings)
            elif action=='hiworks':result=self._collect_hiworks(settings)
            elif action=='folder':result=self.store.sync_folder(settings['folder'])
            elif action=='archive':result=archive_weekly(self.store,settings,force=True)
            elif action=='collect':result=self.collect(settings)
            elif action=='tick':result=self.tick()
            else:result=self.prepare_weekly({**settings,'enabled':True,'auto_send':False},preview=True)
            status='completed'
            if result.get('status') in {'failed','incomplete_sources','partial','delivery_unknown','deferred'} or any(isinstance(v,dict) and v.get('status') in {'failed','partial','incomplete_sources','delivery_unknown','deferred'} for v in result.values()):
                status='attention'
        except Exception as error:
            status='failed';result={'errorType':type(error).__name__,'message':'연결·수집 범위·모델 설정을 확인하세요.'}
        with self.store.transaction() as db:
            db.execute('UPDATE workspace_jobs SET status=?,finished=?,result=? WHERE id=?',
                       (status,now(),json.dumps(result,ensure_ascii=False),identifier))

    def messages(self,offset=0,limit=50,kind=None):
        if type(offset) is not int or offset<0:raise ValidationError('페이지 번호를 확인하세요.')
        settings=self.settings()
        scopes={'outlook':'outlook:'+settings['mailbox'],'teams':'teams:'+settings['mailbox'],'hiworks':'hiworks:'+settings['hiworks_company']}
        if kind is not None and kind not in scopes:raise ValidationError('자료 종류를 확인하세요.')
        chosen=[scopes[kind]] if kind else list(scopes.values())
        with self.store.lock:
            rows=self.store.connection.execute("""SELECT v.* FROM archive_versions v
              JOIN (SELECT scope,source_id,MAX(version) version FROM archive_versions GROUP BY scope,source_id) x
              ON v.scope=x.scope AND v.source_id=x.source_id AND v.version=x.version
              WHERE json_extract(v.metadata,'$.kind')='message' AND v.deleted=0
              AND v.scope IN ("""+','.join('?' for _ in chosen)+") ORDER BY v.id DESC LIMIT ? OFFSET ?",(*chosen,limit+1,offset)).fetchall()
        items=[]
        for row in rows[:limit]:
            message=json.loads(self.store.read_version(row['id']))
            if row['scope']=='teams:'+settings['mailbox'] and message.get('chat') not in settings['teams_chats']:continue
            items.append({'id':row['id'],'scope':row['scope'],'version':row['version'],'observedAt':row['observed_at'],
                **{k:message.get(k) for k in ('provider','subject','sender','timestamp','direction')},
                'body':(message.get('body') or '')[:20000],'message':message})
        return {'items':items,'hasMore':len(rows)>limit,'offset':offset}

    def evidence(self,identifier):
        with self.store.lock:
            row=self.store.connection.execute("SELECT * FROM archive_versions WHERE id=?",(identifier,)).fetchone()
        if row is None or row['deleted']:raise ValidationError('보관본을 찾을 수 없습니다.')
        if json.loads(row['metadata']).get('kind')=='message':
            message=json.loads(self.store.read_version(identifier))
            if message.get('chat') and message['chat'] not in self.settings()['teams_chats']:raise ValidationError('현재 수집 범위 밖의 채팅입니다.')
            body=message.get('body') or message.get('subject')
            provider='outlook' if message.get('provider') in {'outlook','outlook_com'} else ('document' if message.get('provider')=='hiworks' else 'meeting')
        else:
            from .source_text import extract_text
            body=extract_text(self.store.read_version(identifier),row['source_id'],8000);provider='document'
        proof_id='archive-'+str(identifier)+'-'+row['hash'][:16]
        for existing in self.repository.list_evidence():
            if existing['id']==proof_id:return existing
        return self.repository.add_evidence({'id':proof_id,'provider':provider,'sourceId':row['scope']+':'+row['source_id'],
            'capturedAt':row['observed_at'],'evidence':(body or '보관 자료')[:8000],
            'locator':f'보관 자료 {identifier} / 버전 {row["version"]}','sha256':row['hash'],
            'threadId':None,'finality':'unknown','role':'context'},actor_id='user')

    def prepare_weekly(self,settings,at=None,preview=False):
        at=at or datetime.now(timezone.utc)
        if preview:
            _,previous=report_window(at);at=previous+timedelta(days=7)
        messages=self.messages(0,10000)['items']
        def archived_mail(mailbox,start,end):
            selected=[m['message'] for m in messages if m['scope']=='outlook:'+mailbox and m.get('timestamp')
                      and start<=datetime.fromisoformat(m['timestamp'])<end]
            # A local archive is a bounded snapshot, not a guarantee of complete mailbox history.
            return {'messages':selected,'complete':False,'warnings':['mail_archive_snapshot_only']+([] if selected else ['archived_mail_empty'])}
        def archived_teams(chats):
            selected=[];warnings=[]
            for row in messages:
                message=row['message']
                if row['scope']!='teams:'+settings['mailbox'] or message.get('chat') not in chats:continue
                stamp=message.get('timestamp')
                if stamp and not report_window(at)[0]<=datetime.fromisoformat(stamp)<report_window(at)[1]:continue
                if not stamp and not report_window(at)[0]<=datetime.fromisoformat(row['observedAt'])<report_window(at)[1]:continue
                selected.append(message)
            if chats:warnings=['teams_screen_scope_only']+([] if selected else ['archived_teams_empty'])
            return {'messages':selected,'complete':not chats,'warnings':warnings}
        def extra(start,end):
            import re
            sources=[];warnings=[]
            for row in messages:
                if row['scope']!='hiworks:'+settings['hiworks_company']:continue
                m=dict(row['message']);stamp=m.get('timestamp')
                period_events=[]
                for line in m.get('history',[]):
                    for found in re.findall(r'20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?',line):
                        try:event_at=datetime.fromisoformat(found).replace(tzinfo=KST)
                        except ValueError:continue
                        if start<=event_at<end:period_events.append(line);break
                if (stamp and start<=datetime.fromisoformat(stamp)<end) or period_events:
                    m['reportPeriod']={'start':start.isoformat(),'end':end.isoformat()}
                    m['periodHistory']=period_events
                    if not stamp or not start<=datetime.fromisoformat(stamp)<end:
                        m['body']='과거 기안문입니다. 이번 주 실적은 periodHistory의 기간 내 결재 처리만 판단하세요.\n'+m['body']
                    sources.append(m)
                elif start<=datetime.fromisoformat(row['observedAt'])<end:
                    m['contextOnly']=True
                    m['body']='최근 수집한 과거 기안문입니다. 변경 시점은 확인되지 않았으며 이번 주 실적으로 단정하지 마세요.\n'+m['body']
                    sources.append(m)
            if settings['hiworks_enabled'] and not sources:warnings.append('archived_hiworks_empty')
            if settings['hiworks_enabled']:warnings.append('hiworks_archive_snapshot_only')
            if settings['folder']:
                from .source_text import extract_text
                versions=[r for r in self.store.latest('folder:'+canonical(settings['folder'])).values() if not r['deleted'] and r['source_id'].endswith('.xlsx')]
                # Select the newest dated meeting material, retain old versions in the archive.
                import re
                dated=[]
                for row in versions:
                    match=re.search(r'(20\d{6})',Path(row['source_id']).stem)
                    if match:
                        try:day=datetime.strptime(match[1],'%Y%m%d').date()
                        except ValueError:continue
                        if day<=end.astimezone(KST).date():dated.append((day,row))
                if dated:
                    day,row=max(dated,key=lambda r:r[0])
                    try:
                        body=extract_text(self.store.read_version(row['id']),row['source_id'])
                        sources.append({'source_id':'archive:'+str(row['id']),'provider':'document','subject':row['source_id'],
                          'body':'회의 자료 기준일: '+str(day)+'\n'+body,'timestamp':None,'contextOnly':True,
                          'archiveVersionId':row['id'],'archiveHash':row['hash']})
                        if day<start.astimezone(KST).date():warnings.append('previous_meeting_reference_only')
                    except (ValueError,KeyError,OSError):warnings.append('meeting_text_unreadable')
                else:warnings.append('archived_meeting_empty')
            if self.calendar_store:
                month=(end+timedelta(days=3)).strftime('%Y-%m')
                view=self.calendar_store.view(month)
                for event in view['events']:
                    if not event.get('date') or event.get('sourceChanged') or event['status']=='completed':continue
                    if end.date()<datetime.fromisoformat(event['date']).date()<=end.date()+timedelta(days=7):
                        sources.append({'source_id':'calendar:'+event['id'],'provider':'calendar','subject':event['fund']+' '+event['title'],
                          'body':f"차주 일정 후보: {event['date']} / 상태: {event['status']} / 원본: {event['sourceUrl']}",
                          'timestamp':None,'contextOnly':True})
            return sources,warnings
        return run_weekly(self.store,{**settings,'enabled':True},at,mail_collector=archived_mail,teams_collector=archived_teams,
                          extractor=self.reporter,sender=self.sender,extra_sources=extra,
                          identifier_suffix='preview-'+str(uuid4()) if preview else '')

    def start_scheduler(self):
        # The UI never starts collectors implicitly. Windows Task Scheduler owns timed work.
        pass

    def close(self):
        self.stop.set()
        if self.worker:self.worker.join()
