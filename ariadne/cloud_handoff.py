"""Company SharePoint delivery using a dedicated, owner-operated Edge profile.

No OneDrive desktop client, extension, password entry, cookie export or policy
change. Only sealed relay packets enter the explicitly selected company folder.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

from .automation_store import now
from .handoff import (MAGIC, _write_packet, import_packet,
                      initialize as initialize_handoff, status as handoff_status)
from .models import ValidationError

MAX_PACKET = 64 * 1024 * 1024
NAME = re.compile(r'[0-9a-f-]{36}-[0-9]{10}\.ariadne-packet')


class CloudError(ValidationError):
    def __init__(self, code):
        self.code = code
        messages = {
            'login_required': '회사 웹 OneDrive 연결을 다시 열고 회사 계정으로 로그인하세요.',
            'permission_denied': '회사 웹 폴더의 읽기·업로드 권한을 확인하세요. 보안 정책은 변경하지 않습니다.',
            'browser_unavailable': '회사 브라우저 연결을 열지 못했습니다. Edge 설치 또는 회사에서 허용한 연동 방법을 확인하세요.',
            'connection_busy': '회사 웹 연결 창 또는 다른 자료 전달이 사용 중입니다. 창을 닫으면 다음 예약 때 재시도합니다.',
            'connection_failed': '회사 웹 폴더 연결에 실패했습니다. 로컬 DB의 자료는 유지됩니다.',
            'packet_missing': '이전 순번의 전달 파일이 없습니다. 보관한 전달 파일을 복구하세요.',
            'packet_invalid': '전달 파일 형식 또는 크기를 확인하세요. 웹 전달은 파일 하나당 64 MiB까지 지원합니다.',
            'remote_conflict': '회사 웹 폴더의 같은 이름 파일이 원본과 다릅니다. 덮어쓰지 않고 중지했습니다.',
            'sender_conflict': '다른 발신 PC의 전달 파일이 섞여 있습니다. 기존 발신 PC의 자료만 연결하세요.',
            'packet_authentication': '자료 연결 코드 또는 전달 파일의 무결성을 확인하세요. 확인되지 않은 자료는 DB에 반영하지 않습니다.',
        }
        super().__init__(messages.get(code, messages['connection_failed']))


def target(value):
    try:
        url = urlsplit(value)
        folder = parse_qs(url.query, strict_parsing=True).get('id', [])
        valid = (url.scheme == 'https' and re.fullmatch(r'[a-z0-9-]+-my\.sharepoint\.com',url.hostname or '') and url.port in (None,443)
                 and not url.username and not url.password and url.path == '/my'
                 and len(folder)==1 and re.fullmatch(r'/personal/[A-Za-z0-9_]+/Documents/ARIADNE',folder[0]))
    except (ValueError,TypeError):
        valid = False
    if not valid:
        raise ValueError('회사 계정의 지정된 ARIADNE 웹 폴더 주소를 사용하세요.')
    folder=folder[0];origin='https://'+url.hostname
    return {'url':origin+'/my?id='+quote(folder,safe=''),'folder':folder,
            'site':origin+folder.split('/Documents/')[0]}


def validate_url(value):
    return target(value)['url']


def initialize(store):
    initialize_handoff(store)
    with store.lock:
        store.connection.execute('''CREATE TABLE IF NOT EXISTS handoff_cloud_uploads(
        filename TEXT PRIMARY KEY,digest TEXT NOT NULL,verified_at TEXT NOT NULL)''')


def _connection_file(directory):
    return Path(directory)/'cloud-connection.json'


def _connection_state(directory, status, **values):
    path = _connection_file(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,mode='w',encoding='utf-8',delete=False) as handle:
        temporary = Path(handle.name)
        json.dump({'status':status,'checkedAt':now(),**values},handle)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def connection_status(directory):
    connection = {'status':'not_connected'}
    path = _connection_file(directory)
    if path.exists():
        try:
            saved=json.loads(path.read_text(encoding='utf-8'))
            connection={k:saved[k] for k in ('status','checkedAt','code') if k in saved}
        except (OSError,ValueError):
            connection={'status':'not_connected'}
    return connection


def status(store, directory):
    initialize(store)
    with store.lock:
        row = store.connection.execute('SELECT * FROM handoff_cloud_uploads ORDER BY verified_at DESC LIMIT 1').fetchone()
    return {'connection':connection_status(directory),'lastUploaded':dict(row) if row else None}


@contextmanager
def profile_lock(directory):
    """OS releases the byte lock even when a scheduled process is interrupted."""
    path=Path(directory)/'cloud-profile.lock';path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    if path.stat().st_size==0:handle.write(b'1');handle.flush()
    handle.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close();raise CloudError('connection_busy') from None
    try:
        yield
    finally:
        handle.seek(0)
        if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
        handle.close()


class SharePointClient:
    def __init__(self, context, url):
        self.request=context.request
        selected=target(url);self.site,self.folder=selected['site'],selected['folder']

    def _get(self, path):
        try:
            result=self.request.get(self.site+'/_api/'+path,headers={'Accept':'application/json;odata=verbose'},
                                    max_redirects=0,timeout=90000)
        except Exception:
            raise CloudError('connection_failed') from None
        if result.status in (301,302,303,307,308,401):raise CloudError('login_required')
        if result.status==403:raise CloudError('permission_denied')
        return result

    def check_folder(self):
        result=self._get("web/GetFolderByServerRelativeUrl('"+self.folder+"')?$select=ServerRelativeUrl")
        if result.status!=200:raise CloudError('connection_failed')
        try:value=result.json()['d']['ServerRelativeUrl']
        except (ValueError,KeyError,TypeError):raise CloudError('login_required') from None
        if value!=self.folder:raise CloudError('permission_denied')

    def _file_path(self, filename):
        if not NAME.fullmatch(filename):raise CloudError('packet_invalid')
        return "web/GetFileByServerRelativeUrl('"+self.folder+'/'+filename+"')"

    def packet_files(self):
        """List only the selected folder; pagination cannot change host or scope."""
        base="web/GetFolderByServerRelativeUrl('"+self.folder+"')/Files"
        path=base+'?$select=Name,Length&$top=100'
        expected=urlsplit(self.site+'/_api/'+base)
        files={};seen=set()
        for _ in range(201):
            if path in seen:raise CloudError('connection_failed')
            seen.add(path);response=self._get(path)
            if response.status!=200:raise CloudError('connection_failed')
            try:
                data=response.json()['d'];rows=data['results'];following=data.get('__next')
                if not isinstance(rows,list):raise ValueError()
                for row in rows:
                    name=row['Name']
                    if not isinstance(name,str):raise ValueError()
                    if not name.endswith('.ariadne-packet'):continue
                    if not NAME.fullmatch(name) or str(UUID(name[:36]))!=name[:36] or int(name[37:47])<1:
                        raise CloudError('packet_invalid')
                    length=int(row['Length'])
                    if not 44<length<=MAX_PACKET or name in files:raise CloudError('packet_invalid')
                    files[name]=length
                if len(files)>20000:raise ValueError()
                if not following:return files
                link=urlsplit(following)
                if (link.scheme!=expected.scheme or link.netloc!=expected.netloc or link.fragment
                        or unquote(link.path)!=unquote(expected.path)):
                    raise ValueError()
                query=parse_qs(link.query,strict_parsing=True)
                if set(query)-{'$select','$top','$skiptoken'} or query.get('$select')!=['Name,Length']:
                    raise ValueError()
                path=base+'?'+link.query
            except CloudError:raise
            except (ValueError,KeyError,TypeError,AttributeError):
                raise CloudError('connection_failed') from None
        raise CloudError('connection_failed')

    def download_packet(self, filename, length):
        response=self._get(self._file_path(filename)+'/$value')
        if response.status==404:raise CloudError('packet_missing')
        if response.status!=200:raise CloudError('connection_failed')
        payload=response.body()
        if len(payload)!=length or not 44<len(payload)<=MAX_PACKET or not payload.startswith(MAGIC):
            raise CloudError('packet_invalid')
        return payload

    def remote_matches(self, filename, payload):
        path=self._file_path(filename)
        meta=self._get(path+'?$select=Length')
        if meta.status==404:return False
        if meta.status!=200:raise CloudError('connection_failed')
        try:length=int(meta.json()['d']['Length'])
        except (ValueError,KeyError,TypeError):raise CloudError('connection_failed') from None
        if length!=len(payload):raise CloudError('remote_conflict')
        data=self._get(path+'/$value')
        if data.status!=200:raise CloudError('connection_failed')
        if hashlib.sha256(data.body()).digest()!=hashlib.sha256(payload).digest():
            raise CloudError('remote_conflict')
        return True

    def put_verified(self, filename, payload):
        if not NAME.fullmatch(filename) or not payload.startswith(MAGIC) or not 44<len(payload)<=MAX_PACKET:
            raise CloudError('packet_invalid')
        if self.remote_matches(filename,payload):return
        try:
            response=self.request.post(self.site+'/_api/contextinfo',headers={'Accept':'application/json;odata=verbose'},
                                       max_redirects=0,timeout=90000,data=b'')
            if response.status in (301,302,303,307,308,401):raise CloudError('login_required')
            if response.status==403:raise CloudError('permission_denied')
            if response.status!=200:raise CloudError('connection_failed')
            digest=response.json()['d']['GetContextWebInformation']['FormDigestValue']
            if not isinstance(digest,str) or not digest:raise CloudError('login_required')
            path="web/GetFolderByServerRelativeUrl('"+self.folder+"')/Files/add(url='"+filename+"',overwrite=false)"
            response=self.request.post(self.site+'/_api/'+path,data=payload,
                headers={'Accept':'application/json;odata=verbose','Content-Type':'application/octet-stream','X-RequestDigest':digest},
                max_redirects=0,timeout=120000)
            if response.status==403:raise CloudError('permission_denied')
            if response.status in (301,302,303,307,308,401):raise CloudError('login_required')
            if response.status not in (200,201):raise CloudError('connection_failed')
        except CloudError:
            raise
        except Exception:
            # Outcome may be unknown. Retry first checks the existing remote bytes.
            raise CloudError('connection_failed') from None
        if not self.remote_matches(filename,payload):raise CloudError('connection_failed')


@contextmanager
def browser_client(directory, url, *, visible=False):
    url=validate_url(url)
    with profile_lock(directory):
        try:
            from playwright.sync_api import sync_playwright
            manager=sync_playwright().start()
        except Exception:
            raise CloudError('browser_unavailable') from None
        context=None
        try:
            try:
                context=manager.chromium.launch_persistent_context(str(Path(directory)/'company-web-profile'),
                    channel='msedge',headless=not visible,chromium_sandbox=True,no_viewport=True)
                page=context.pages[0] if context.pages else context.new_page()
                page.goto(url,wait_until='domcontentloaded',timeout=90000)
            except Exception:
                raise CloudError('browser_unavailable') from None
            yield SharePointClient(context,url),page
        finally:
            try:
                if context:context.close()
            finally:
                manager.stop()


def upload_packets(store, directory, url, client):
    validate_url(url);initialize(store)
    spool=Path(directory)/'outgoing-packets'
    with store.lock:
        stream=store.connection.execute('SELECT stream FROM handoff_stream').fetchone()
        rows=store.connection.execute("SELECT sequence FROM handoff_outbox WHERE status='published' ORDER BY sequence").fetchall()
    if not stream:return {'status':'waiting','packets':0}
    client.check_folder();count=0
    for row in rows:
        name=f'{stream["stream"]}-{row["sequence"]:010d}.ariadne-packet'
        path=spool/name
        if path.is_symlink() or not path.is_file():raise CloudError('packet_missing')
        if not 44<path.stat().st_size<=MAX_PACKET:raise CloudError('packet_invalid')
        payload=path.read_bytes();digest=hashlib.sha256(payload).hexdigest()
        if not payload.startswith(MAGIC):raise CloudError('packet_invalid')
        with store.lock:
            previous=store.connection.execute('SELECT digest FROM handoff_cloud_uploads WHERE filename=?',(name,)).fetchone()
        if previous:
            if previous['digest']!=digest:raise CloudError('packet_invalid')
            continue
        client.put_verified(name,payload)
        with store.transaction() as db:
            db.execute('INSERT INTO handoff_cloud_uploads VALUES(?,?,?)',(name,digest,now()))
        count+=1
    _connection_state(directory,'connected')
    return {'status':'uploaded','packets':count,'verified':True}


def deliver(store, directory, url):
    try:
        with browser_client(directory,url) as (client,_):
            return upload_packets(store,directory,url,client)
    except CloudError as error:
        _connection_state(directory,'attention',code=error.code)
        return {'status':'failed','code':error.code,'message':str(error)}
    except Exception:
        _connection_state(directory,'attention',code='connection_failed')
        return {'status':'failed','code':'connection_failed','message':str(CloudError('connection_failed'))}


def download_packets(store, directory, url, cache, key, client, *, limit=20):
    """Receive in source order. Authenticated imports, never cloud writes."""
    validate_url(url);initialize(store);client.check_folder()
    files=client.packet_files()
    with store.lock:
        prior=store.connection.execute('SELECT * FROM handoff_imports ORDER BY sequence DESC LIMIT 1').fetchone()
    streams={name[:36] for name in files}
    if len(streams)>1 or prior and streams and streams!={prior['stream']}:
        raise CloudError('sender_conflict')
    sequence=prior['sequence'] if prior else 0
    pending=sorted((name for name in files if int(name[37:47])>sequence),key=lambda name:int(name[37:47]))
    # Check the entire advertised sequence before consuming a partial backlog.
    if any(int(name[37:47])!=sequence+index+1 for index,name in enumerate(pending)):
        raise CloudError('packet_missing')
    spool=Path(directory)/'incoming-packets'
    if spool.is_symlink():raise CloudError('packet_invalid')
    spool.mkdir(parents=True,exist_ok=True);count=0
    for name in pending[:limit]:
        payload=client.download_packet(name,files[name]);path=spool/name
        if path.is_symlink():raise CloudError('packet_invalid')
        if path.exists():
            if not path.is_file() or path.stat().st_size!=len(payload) or path.read_bytes()!=payload:
                raise CloudError('remote_conflict')
        else:_write_packet(path,payload)
        try:
            result=import_packet(store,path,cache,key,expected_identity=(name[:36],int(name[37:47])))
        except Exception:
            raise CloudError('packet_authentication') from None
        count+=result['status']=='imported'
    _connection_state(directory,'connected')
    return {'status':'imported' if count else 'waiting','packets':count,
            'pending':max(0,len(pending)-limit),**handoff_status(store)}


def receive(store, directory, url, cache, key):
    try:
        with browser_client(directory,url) as (client,_):
            return download_packets(store,directory,url,cache,key,client)
    except CloudError as error:
        _connection_state(directory,'attention',code=error.code)
        return {'status':'failed','code':error.code,'message':str(error)}
    except Exception:
        _connection_state(directory,'attention',code='connection_failed')
        return {'status':'failed','code':'connection_failed','message':str(CloudError('connection_failed'))}


def open_connection(directory, url):
    validate_url(url)
    if os.name!='nt':raise CloudError('browser_unavailable')
    from .workspace import process_alive
    path=Path(directory)/'cloud-connect.pid'
    if path.exists():
        try:pid=int(path.read_text())
        except ValueError:pid=None
        if pid and time.time()-path.stat().st_mtime<1200 and process_alive(pid):raise CloudError('connection_busy')
    _connection_state(directory,'opening')
    process=subprocess.Popen([sys.executable,'-m','ariadne.cloud_handoff','--data-dir',str(directory),'connect'],
        cwd=Path(__file__).resolve().parent.parent,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    path.write_text(str(process.pid),encoding='ascii')
    return {'status':'opening'}


def connect(directory, url):
    """Open only the authorized folder. The human completes sign-in/MFA."""
    try:
        with browser_client(directory,url,visible=True) as (client,page):
            _connection_state(directory,'login_required')
            deadline=time.monotonic()+900
            while time.monotonic()<deadline:
                if page.is_closed():raise CloudError('login_required')
                try:
                    client.check_folder();_connection_state(directory,'connected');return
                except CloudError as error:
                    if error.code not in ('login_required','permission_denied','connection_failed'):raise
                time.sleep(3)
            raise CloudError('login_required')
    except CloudError as error:
        _connection_state(directory,'attention',code=error.code)
    except Exception:
        _connection_state(directory,'attention',code='browser_unavailable')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',required=True,type=Path)
    parser.add_argument('command',choices=['connect'])
    args=parser.parse_args()
    from .local_settings import load_settings
    config=load_settings(args.data_dir/'automation.json')
    if config['device_role'] not in {'company-relay','omen'} or config['handoff_transport']!='web':
        parser.error('Company web delivery is not configured')
    connect(args.data_dir,config['handoff_web_url'])
