"""Unified collection -> durable evidence -> calendar/report workflows."""
from datetime import datetime, timedelta, timezone
import hashlib
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

    def _claim_operation(self,action):
        """Share the durable GUI/scheduler lock with synchronous validation work."""
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
        return identifier

    def _finish_operation(self,identifier,result,status='completed'):
        if status=='completed' and result.get('status') in {
                'failed','incomplete_sources','scope_acknowledgement_required','scope_confirmation_required','partial',
                'delivery_unknown','delivery_pending','delivery_review_required','deferred'}:
            status='attention'
        with self.store.transaction() as db:
            db.execute('UPDATE workspace_jobs SET status=?,finished=?,result=? WHERE id=?',
                       (status,now(),json.dumps(result,ensure_ascii=False),identifier))

    def start(self,action):
        if action not in {'mail','teams','hiworks','folder','weekly','collect','tick','archive'}:
            raise ValidationError('지원하지 않는 작업입니다.')
        with self.lock:
            settings=self.settings()
            role=settings.get('device_role','single')
            if role=='company-relay' and action not in {'folder','archive','collect','tick'}:
                raise ValidationError('회사 PC는 공유폴더 보관·전달만 실행합니다. 이 작업은 오멘에서 실행하세요.')
            if role=='omen' and action=='archive':
                raise ValidationError('오멘은 회사 PC가 만든 금요일 보관본을 받습니다. 회사 PC에서 원본을 보관하세요.')
            if action in {'mail','teams'} and not settings['mailbox']:raise ValidationError('수집 메일 계정을 저장하세요.')
            if action=='teams' and not settings['teams_chats']:raise ValidationError('Teams 대화 이름을 저장하세요.')
            if action in {'folder','archive'} and not settings['folder']:raise ValidationError('수집 폴더를 저장하세요.')
            identifier=self._claim_operation(action)
            self.worker=threading.Thread(target=self._run,args=(identifier,action,settings),daemon=True)
            self.worker.start()
        return {'id':identifier,'status':'running'}

    def _collect_mail(self,settings,at=None):
        end=at or datetime.now(timezone.utc)
        start=min(end-timedelta(days=7),report_window(end)[0])
        scope='outlook:'+settings['mailbox']
        try:
            result=self.mail(settings['mailbox'],start,end)
            count=self.store.ingest_messages(scope,result['messages'])
        except Exception:
            self._record_collection(scope,{'complete':False,'warnings':['mail_collection_failed']},start,end)
            raise
        quality=self._record_collection(scope,result,start,end)
        return {'read':len(result['messages']),'stored':count,'complete':bool(result.get('complete')),
                'warnings':quality['warnings'],'limitations':quality['limitations']}

    def _collect_teams(self,settings,at=None):
        end=at or datetime.now(timezone.utc);start=end-timedelta(days=7)
        scope='teams:'+settings['mailbox']
        try:
            result=self.teams(settings['teams_chats'],start,end)
            if any(m.get('chat') not in settings['teams_chats'] for m in result['messages']):
                raise ValidationError('Teams 수집 범위가 설정한 대화와 다릅니다.')
            count=self.store.ingest_messages(scope,result['messages'])
        except Exception:
            self._record_collection(scope,{'complete':False,'warnings':['teams_collection_failed']},
                                    start,end,chats=settings['teams_chats'])
            raise
        quality=self._record_collection(scope,result,start,end,chats=settings['teams_chats'])
        return {'status':'deferred' if 'teams_in_use_collection_deferred' in result.get('warnings',[]) else 'partial',
                'read':len(result['messages']),'stored':count,'complete':bool(result.get('complete')),
                'warnings':quality['warnings'],'limitations':quality['limitations'],'coverage':result.get('coverage',[])}

    def _collect_hiworks(self,settings,at=None):
        if not self.bridge:raise ValidationError('브라우저 수집 연결이 없습니다.')
        end=at or datetime.now(timezone.utc)
        start=min(end-timedelta(days=7),report_window(end)[0])
        scope='hiworks:'+settings['hiworks_company']
        try:result=self.bridge.collect(settings['hiworks_company'])
        except Exception:
            self._record_collection(scope,{'complete':False,'warnings':['hiworks_collection_failed']},start,end)
            raise
        quality=self._record_collection(scope,result,start,end)
        return {**result,'warnings':quality['warnings'],'limitations':quality['limitations']}

    def _record_collection(self,scope,result,start,end,chats=None):
        """Record completeness only within the declared scope, separate from limitations."""
        provider=scope.split(':',1)[0]
        known={'teams_screen_scope_only','teams_message_dates_unverified'} if provider=='teams' else set()
        raw_warnings=list(result.get('warnings',[]))
        limitations=list(result.get('limitations',[]))
        warnings=[warning for warning in raw_warnings if warning not in known]
        if result.get('status') in {'failed','partial','deferred'} and not warnings:
            warnings.append(provider+'_collection_incomplete')
        limitations.extend(warning for warning in raw_warnings if warning in known)
        if provider=='outlook':
            limitations.extend(['mail_archive_snapshot_only','mail_attachment_contents_not_collected'])
        elif provider=='teams':limitations.append('teams_screen_scope_only')
        else:limitations.append('hiworks_archive_snapshot_only')
        bounded=result.get('complete') is True
        if (provider=='teams' and result.get('complete') is False
                and 'teams_screen_scope_only' in raw_warnings and not warnings):
            bounded=True
        latest=self.store.latest(scope)
        identifiers=([m['source_id'] for m in result['messages']] if 'messages' in result else
                     [key for key,row in latest.items() if not row['deleted']])
        summary={'kind':'message_collection','periodStart':start.isoformat(),'periodEnd':end.isoformat(),
                 'complete':result.get('complete') is True,'scopeComplete':bounded and not warnings,
                 'warnings':warnings,'limitations':sorted(set(limitations)),
                 'versionIds':[latest[key]['id'] for key in identifiers],
                 'count':len(identifiers)}
        if chats is not None:summary['chatScope']=list(chats)
        with self.store.transaction() as db:
            db.execute('INSERT INTO archive_scans VALUES(?,?,?,?)',
                       (str(uuid4()),scope,now(),json.dumps(summary,ensure_ascii=False)))
        return summary

    def _archive_quality(self,scope,start,end,chats=None):
        provider=scope.split(':',1)[0]
        with self.store.lock:
            rows=self.store.connection.execute('SELECT summary FROM archive_scans WHERE scope=? ORDER BY completed_at DESC,rowid DESC',
                                               (scope,)).fetchall()
        for row in rows:
            receipt=json.loads(row['summary'])
            if receipt.get('kind')!='message_collection':continue
            if chats is not None and set(receipt.get('chatScope',[]))!=set(chats):continue
            try:
                receipt_start=datetime.fromisoformat(receipt['periodStart'])
                receipt_end=datetime.fromisoformat(receipt['periodEnd'])
                if receipt_start.tzinfo is None or receipt_end.tzinfo is None or receipt_start>=receipt_end:continue
                # Teams provides a recent screen snapshot, never full-week history.
                covers=(start<=receipt_end and end-timedelta(days=1)<=receipt_end<=end
                        if provider=='teams' else receipt_start<=start and receipt_end>=end)
            except (ValueError,KeyError,TypeError):continue
            if not covers:continue
            warnings=list(receipt.get('warnings',[]))
            complete=receipt.get('scopeComplete') is True and not warnings
            if not complete and not warnings:warnings.append(provider+'_archive_collection_incomplete')
            if len(receipt.get('versionIds',[]))>10000:
                warnings.append('archive_message_limit_exceeded');complete=False
            return {'complete':complete,'warnings':warnings,'limitations':receipt.get('limitations',[]),
                    'versionIds':receipt.get('versionIds',[])}
        return {'complete':False,'warnings':[provider+'_archive_collection_unverified'],'limitations':[]}

    def _receipt_messages(self,scope,quality,fallback):
        """Freeze the exact versions proven by a collection receipt."""
        if 'versionIds' not in quality:return fallback
        rows=[]
        for identifier in quality['versionIds'][:10000]:
            with self.store.lock:
                record=self.store.connection.execute(
                    'SELECT scope,metadata,observed_at,deleted FROM archive_versions WHERE id=?',(identifier,)).fetchone()
            if (record is None or record['scope']!=scope or record['deleted']
                    or json.loads(record['metadata']).get('kind')!='message'):
                raise ValidationError('수집 확인 기록과 보관 자료가 일치하지 않습니다.')
            message=json.loads(self.store.read_version(identifier))
            rows.append({'scope':scope,'observedAt':record['observed_at'],'timestamp':message.get('timestamp'),'message':message})
        return rows

    def collect(self,settings,at=None):
        result={}
        role=settings.get('device_role','single')
        if role=='omen':
            try:result['handoff']=self._import_handoff(settings)
            except ValidationError as error:result['handoff']={'status':'failed','message':str(error)}
            except Exception:result['handoff']={'status':'failed','message':'회사 자료 연결 폴더·연결 코드·전달 순서를 확인하세요.'}
        operations=[('folder',lambda:self.store.sync_folder(settings['folder'])),
                    ('mail',lambda:self._collect_mail(settings,at)),
                    ('hiworks',lambda:self._collect_hiworks(settings,at))]
        configured=[bool(settings['folder']) and role!='omen',bool(settings['mailbox']),settings['hiworks_enabled']]
        for (name,operation),enabled in zip(operations,configured):
            if not enabled:result[name]={'status':'not_configured','complete':False};continue
            try:result[name]={'status':'collected',**operation()}
            except Exception as error:
                result[name]={'status':'failed','complete':False,'errorType':type(error).__name__,
                              'message':'연결 또는 수집 범위를 확인하세요.'}
        return result

    def _import_handoff(self,settings):
        from .handoff import import_available,read_key
        cache=self.settings_path.parent/'shared-meetings'
        if canonical(settings['folder'])!=canonical(cache):raise ValidationError('오멘의 회의 자료 보관 경로는 이 PC의 shared-meetings 폴더를 사용하세요.')
        if settings.get('handoff_transport')=='web':
            from .cloud_handoff import receive
            result=receive(self.store,self.settings_path.parent,settings['handoff_web_url'],cache,read_key(self.settings_path.parent))
            if result['status']=='failed':raise ValidationError(result['message'])
            return result
        if not settings['handoff_dir']:raise ValidationError('회사 OneDrive 전달 폴더를 저장하세요.')
        return import_available(self.store,settings['handoff_dir'],cache,read_key(self.settings_path.parent))

    def _export_handoff(self,settings):
        from .handoff import export_folder,read_key,install_key
        directory=self.settings_path.parent
        if not (directory/'credentials/shared-folder.dpapi').exists():install_key(directory,os.urandom(32))
        web=settings.get('handoff_transport')=='web'
        destination=directory/'outgoing-packets' if web else settings['handoff_dir']
        if web:destination.mkdir(parents=True,exist_ok=True)
        result=export_folder(self.store,settings['folder'],destination,read_key(directory))
        if web:
            from .cloud_handoff import deliver
            cloud=deliver(self.store,directory,settings['handoff_web_url'])
            result={**result,'cloud':cloud,'status':cloud['status']}
            if cloud.get('message'):result['message']=cloud['message']
        return result

    @staticmethod
    def handoff_configured(settings):
        return bool(settings.get('handoff_web_url') if settings.get('handoff_transport')=='web' else settings['handoff_dir'])

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
                except Exception as error:result['weekly']={'status':'failed','errorType':type(error).__name__,
                    'message':str(error) if isinstance(error,ValidationError) else '보고 생성 전 연결과 자료 범위를 확인하세요.'}
        if settings.get('device_role')=='company-relay' and self.handoff_configured(settings) and (
            result.get('collection',{}).get('folder',{}).get('status')=='collected' or result.get('archive',{}).get('status')=='completed'):
            try:result['handoff']=self._export_handoff(settings)
            except Exception:result['handoff']={'status':'failed','message':'공유 폴더는 보관했지만 OneDrive 전달에 실패했습니다. 다음 실행 때 다시 전달합니다.'}
        return result or {'status':'not_due'}

    def _run(self,identifier,action,settings):
        try:
            if action=='mail':result=self._collect_mail(settings)
            elif action=='teams':result=self._collect_teams(settings)
            elif action=='hiworks':result=self._collect_hiworks(settings)
            elif action=='folder':result=self._import_handoff(settings) if settings.get('device_role')=='omen' else self.store.sync_folder(settings['folder'])
            elif action=='archive':result=archive_weekly(self.store,settings,force=True)
            elif action=='collect':result=self.collect(settings)
            elif action=='tick':result=self.tick()
            else:result=self.prepare_weekly({**settings,'enabled':True,'auto_send':False},preview=True)
            if settings.get('device_role')=='company-relay' and self.handoff_configured(settings) and action in {'folder','archive','collect'}:
                result['handoff']=self._export_handoff(settings)
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

    def _validation_operation(self,action,operation):
        with self.lock:
            settings=self.settings()
            if settings.get('device_role')!='omen':
                raise ValidationError('운영 검증은 오멘 장치에서 실행하세요.')
            if not settings.get('mailbox'):
                raise ValidationError('보고서를 받을 본인 회사 메일 주소를 설정하세요.')
            identifier=self._claim_operation(action)
            try:result=operation(settings)
            except Exception as error:
                self._finish_operation(identifier,{'errorType':type(error).__name__,
                    'message':'운영 검증 설정과 자료 수집 결과를 확인하세요.'},'failed')
                raise
            self._finish_operation(identifier,result)
            return result

    def prepare_validation(self,scope_acknowledged=False):
        if type(scope_acknowledged) is not bool:raise ValidationError('자료 범위 확인 여부를 확인하세요.')
        return self._validation_operation('validation_prepare',lambda settings:
            self.prepare_weekly(settings,validation=True,scope_acknowledged=scope_acknowledged))

    @staticmethod
    def _validation_owner(settings,identifier):
        import re
        suffix=hashlib.sha256(settings['mailbox'].strip().casefold().encode()).hexdigest()[:12]
        pattern=r'\d{4}-\d{2}-\d{2}-'+suffix+r'-validation-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
        if not isinstance(identifier,str) or not re.fullmatch(pattern,identifier):
            raise ValidationError('현재 본인 계정의 운영 검증 실행번호를 선택하세요.')

    def send_validation(self,identifier,expected_hash):
        def operation(settings):
            self._validation_owner(settings,identifier)
            from .weekly_worker import send_prepared
            return send_prepared(self.store,settings,identifier,expected_hash,sender=self.sender)
        return self._validation_operation('validation_send',operation)

    def confirm_validation(self,identifier):
        def operation(settings):
            self._validation_owner(settings,identifier)
            from .weekly_worker import confirm_delivery_run
            return confirm_delivery_run(self.store,settings,identifier)
        return self._validation_operation('validation_confirm',operation)

    def prepare_weekly(self,settings,at=None,preview=False,validation=False,scope_acknowledged=False):
        at=at or datetime.now(timezone.utc)
        if type(validation) is not bool or type(scope_acknowledged) is not bool:
            raise ValidationError('운영 검증 설정을 확인하세요.')
        if validation and (preview or settings.get('device_role')!='omen'):
            raise ValidationError('운영 검증은 오멘의 실제 금요일 보관본으로 실행하세요.')
        if validation and not settings.get('report_template'):
            raise ValidationError('운영 검증에 사용할 회사 회의 Excel 양식을 설정하세요.')
        snapshot=None;template_bytes=None
        if settings.get('device_role')=='omen':
            try:self._import_handoff(settings)
            except Exception:raise ValidationError('회사 회의 자료를 받지 못했습니다. 회사 웹 연결 또는 동기화 폴더·자료 연결 코드를 확인하세요.') from None
            if validation or settings['handoff_required'] and not preview:
                expected=report_window(at)[1]
                scope='folder:'+canonical(settings['folder'])
                weeks=[r for r in archive_runs(self.store) if r['scope']==scope and r['status']=='completed' and datetime.fromisoformat(r['week_end'])==expected]
                if not weeks:raise ValidationError('이번 금요일 16시 회사 보관본이 도착하지 않아 보고 생성·발송을 기다립니다.')
                from .weekly_archive import archive_files
                snapshot=archive_files(self.store,weeks[0]['id'])
                if settings['report_template']:
                    try:relative=Path(settings['report_template']).resolve().relative_to(Path(settings['folder']).resolve()).as_posix()
                    except ValueError:raise ValidationError('회사 회의 양식은 받은 회의 자료 폴더 안에서 선택하세요.') from None
                    template_row=next((r for r in snapshot if r['source_id']==relative),None)
                    if template_row is None:raise ValidationError('이번 금요일 보관본에 회의 양식이 없습니다.')
                    template_bytes=self.store.read_version(template_row['id'])
        if preview:
            _,previous=report_window(at);at=previous+timedelta(days=7)
        archived=self.messages(0,10000)
        messages=archived['items']
        start,end=report_window(at)
        mail_scope='outlook:'+settings['mailbox']
        teams_scope='teams:'+settings['mailbox']
        hiworks_scope='hiworks:'+settings['hiworks_company']
        mail_quality=self._archive_quality(mail_scope,start,end)
        teams_quality=(self._archive_quality(teams_scope,start,end,settings['teams_chats'])
                       if settings['teams_chats'] else {'complete':True,'warnings':[],'limitations':[]})
        hiworks_quality=(self._archive_quality(hiworks_scope,start,end)
                         if settings['hiworks_enabled'] else {'complete':True,'warnings':[],'limitations':[]})
        # Pinned receipts have their own input limit; this page bounds fallback history only.
        if archived['hasMore'] and 'versionIds' not in mail_quality:
            mail_quality={**mail_quality,'complete':False,'warnings':mail_quality['warnings']+['archive_message_limit_exceeded']}
        mail_rows=self._receipt_messages(mail_scope,mail_quality,messages)
        teams_rows=self._receipt_messages(teams_scope,teams_quality,messages)
        hiworks_rows=self._receipt_messages(hiworks_scope,hiworks_quality,messages)
        def archived_mail(mailbox,start,end):
            selected=[m['message'] for m in mail_rows if m['scope']=='outlook:'+mailbox and m.get('timestamp')
                      and start<=datetime.fromisoformat(m['timestamp'])<end]
            return {'messages':selected,**mail_quality,'archiveSnapshot':True}
        def archived_teams(chats):
            selected=[];limitations=list(teams_quality['limitations'])
            for row in teams_rows:
                message=dict(row['message'])
                if row['scope']!='teams:'+settings['mailbox'] or message.get('chat') not in chats:continue
                stamp=message.get('timestamp')
                if stamp and not report_window(at)[0]<=datetime.fromisoformat(stamp)<report_window(at)[1]:continue
                if not stamp and not report_window(at)[0]<=datetime.fromisoformat(row['observedAt'])<report_window(at)[1]:continue
                if not stamp:
                    message['contextOnly']=True
                    limitations.append('teams_message_dates_unverified')
                selected.append(message)
            return {'messages':selected,**teams_quality,'limitations':sorted(set(limitations)),'archiveSnapshot':True}
        def extra(start,end):
            import re
            sources=[];warnings=list(hiworks_quality['warnings']);limitations=list(hiworks_quality['limitations'])
            if not hiworks_quality['complete'] and not warnings:warnings.append('hiworks_archive_collection_incomplete')
            for row in hiworks_rows:
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
            if settings['hiworks_enabled'] and not sources:
                if hiworks_quality['complete']:limitations.append('hiworks_current_period_evidence_empty')
                else:warnings.append('archived_hiworks_empty')
            if settings['folder']:
                from .source_text import extract_text
                versions=([r for r in snapshot if r['source_id'].endswith('.xlsx')] if snapshot is not None else
                    [r for r in self.store.latest('folder:'+canonical(settings['folder'])).values() if not r['deleted'] and r['source_id'].endswith('.xlsx')])
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
                        if day<start.astimezone(KST).date():limitations.append('previous_meeting_reference_only')
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
            return sources,warnings,limitations
        suffix=('validation-' if validation else 'preview-' if preview else '')
        suffix+=str(uuid4()) if suffix else ''
        return run_weekly(self.store,{**settings,'enabled':True,**({'auto_send':False} if validation else {})},at,
                          mail_collector=archived_mail,teams_collector=archived_teams,
                          extractor=self.reporter,sender=self.sender,extra_sources=extra,
                          identifier_suffix=suffix,template_bytes=template_bytes,strict_sources=validation,
                          scope_acknowledged=scope_acknowledged)

    def start_scheduler(self):
        # The UI never starts collectors implicitly. Windows Task Scheduler owns timed work.
        pass

    def close(self):
        self.stop.set()
        if self.worker:self.worker.join()
