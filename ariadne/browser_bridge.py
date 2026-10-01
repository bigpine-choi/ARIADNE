"""Authenticated, loopback-only browser extension bridge; no cookie/token access."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
from uuid import uuid4

from .credentials import _crypt
from .hiworks_archive import archive_detail, safe_url
from .models import ValidationError
from .automation_store import now


def extension_origin(origin):
    """Accept only native Whale and Chromium extension origins, never web pages."""
    return re.fullmatch(r'(?:chrome|whale)-extension://[a-p]{32}',origin) is not None


class BrowserBridge:
    def __init__(self,store,data_dir):
        self.store,self.path=store,Path(data_dir)/'credentials/browser-bridge.dpapi'
        self.server=None; self.thread=None
        with store.lock:
            store.connection.executescript("""CREATE TABLE IF NOT EXISTS browser_jobs(
            id TEXT PRIMARY KEY,company TEXT NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL,
            deadline TEXT NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS browser_presence(id INTEGER PRIMARY KEY CHECK(id=1),seen TEXT NOT NULL);""")
            store.connection.executescript("""CREATE TABLE IF NOT EXISTS hiworks_checks(
            company TEXT NOT NULL,document_id TEXT NOT NULL,checked_at TEXT NOT NULL,
            PRIMARY KEY(company,document_id));""")

    def token(self,create=False):
        if not self.path.exists():
            if not create: return None
            self.path.parent.mkdir(parents=True,exist_ok=True)
            token=secrets.token_urlsafe(32)
            temporary=self.path.with_suffix('.tmp')
            temporary.write_bytes(_crypt(token.encode()))
            temporary.replace(self.path)
            return token
        return _crypt(self.path.read_bytes(),decrypt=True).decode('ascii')

    def status(self):
        with self.store.lock:
            row=self.store.connection.execute('SELECT seen FROM browser_presence WHERE id=1').fetchone()
        return {'paired':self.path.exists(),'lastSeen':row['seen'] if row else None}

    def collect(self,company,timeout=300):
        from .local_settings import validate_settings
        validate_settings({'hiworks_company':company})
        if not company: raise ValidationError('하이웍스 회사 도메인을 저장하세요.')
        if not self.token(): raise ValidationError('브라우저 수집 연결을 먼저 등록하세요.')
        identifier=str(uuid4())
        deadline=(datetime.now(timezone.utc)+timedelta(seconds=timeout)).isoformat()
        with self.store.transaction() as db:
            db.execute('INSERT INTO browser_jobs VALUES(?,?,?, ?,?,?)',
                       (identifier,company,'queued',now(),deadline,json.dumps({'documents':0,'versions':0})))
        started=time.monotonic()
        while time.monotonic()-started<timeout:
            with self.store.lock:
                row=self.store.connection.execute('SELECT status,result FROM browser_jobs WHERE id=?',(identifier,)).fetchone()
            if row['status'] in {'completed','partial','failed'}:
                return {'status':row['status'],**json.loads(row['result'])}
            time.sleep(.5)
        with self.store.transaction() as db:
            db.execute("UPDATE browser_jobs SET status='failed' WHERE id=? AND status IN ('queued','running')",(identifier,))
        return {'status':'failed','complete':False,'warnings':['browser_extension_not_connected_or_timed_out']}

    def next(self):
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO browser_presence VALUES(1,?)",(now(),))
            db.execute("UPDATE browser_jobs SET status='failed' WHERE deadline<? AND status IN ('queued','running')",(now(),))
            row=db.execute("SELECT * FROM browser_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None: return {'job':None}
            db.execute("UPDATE browser_jobs SET status='running' WHERE id=?",(row['id'],))
        base='https://approval.office.hiworks.com/'+row['company']+'/approval/document/'
        with self.store.lock:
            previous={r['document_id']:r['checked_at'] for r in self.store.connection.execute(
                'SELECT document_id,checked_at FROM hiworks_checks WHERE company=? ORDER BY checked_at DESC LIMIT 4000',(row['company'],))}
        return {'job':{'id':row['id'],'company':row['company'],'deadline':row['deadline'],
          'lists':[base+suffix for suffix in ('department_box/all','box/writer','box/approval','box/return','lists/A')],
          'maxPages':20,'maxDocuments':200,'previousChecks':previous}}

    def accept(self,value):
        if not isinstance(value,dict) or set(value)!={'jobId','page'}: raise ValidationError('수집 요청 형식을 확인하세요.')
        with self.store.lock:
            row=self.store.connection.execute("SELECT * FROM browser_jobs WHERE id=? AND status='running' AND deadline>=?",(value['jobId'],now())).fetchone()
            if row is None: raise ValidationError('진행 중인 수집 요청을 찾을 수 없습니다.')
            result=archive_detail(self.store,value['page'],row['company'])
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM browser_jobs WHERE id=?',(value['jobId'],)).fetchone()
            totals=json.loads(row['result']);totals['documents']+=1;totals['versions']+=result['stored']
            db.execute('INSERT OR REPLACE INTO hiworks_checks VALUES(?,?,?)',(row['company'],value['page']['documentId'],now()))
            db.execute('UPDATE browser_jobs SET result=? WHERE id=?',(json.dumps(totals),row['id']))
        return result

    def finish(self,value):
        if not isinstance(value,dict) or set(value)!={'jobId','complete','warnings'} or type(value['complete']) is not bool:
            raise ValidationError('수집 결과 형식을 확인하세요.')
        if not isinstance(value['warnings'],list) or len(value['warnings'])>30 or any(not isinstance(w,str) or not re.fullmatch(r'[a-z_]{1,80}',w) for w in value['warnings']):
            raise ValidationError('수집 경고 형식을 확인하세요.')
        with self.store.transaction() as db:
            row=db.execute("SELECT * FROM browser_jobs WHERE id=? AND status='running' AND deadline>=?",(value['jobId'],now())).fetchone()
            if row is None: raise ValidationError('수집 요청이 만료되었습니다.')
            result={**json.loads(row['result']),'complete':value['complete'],'warnings':value['warnings']}
            state='completed' if value['complete'] else ('partial' if result['documents'] else 'failed')
            db.execute('UPDATE browser_jobs SET status=?,result=? WHERE id=?',(state,json.dumps(result),row['id']))
        return result

    def start(self,port=8767):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,code,data):
                payload=json.dumps(data,ensure_ascii=False).encode()
                self.send_response(code)
                origin=self.headers.get('Origin','')
                if extension_origin(origin):
                    self.send_header('Access-Control-Allow-Origin',origin)
                    self.send_header('Vary','Origin')
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(payload)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.end_headers(); self.wfile.write(payload)
            def authorized(self):
                origin=self.headers.get('Origin','')
                host=self.headers.get('Host')
                token=owner.token()
                return host==f'127.0.0.1:{owner.server.server_port}' and extension_origin(origin) and token is not None and hmac.compare_digest(self.headers.get('X-Ariadne-Bridge',''),token)
            def do_OPTIONS(self):
                origin=self.headers.get('Origin','')
                if not extension_origin(origin):return self.reply(403,{'error':'denied'})
                self.send_response(204); self.send_header('Access-Control-Allow-Origin',origin)
                self.send_header('Access-Control-Allow-Headers','Content-Type,X-Ariadne-Bridge')
                self.send_header('Access-Control-Allow-Methods','GET,POST')
                self.end_headers()
            def do_GET(self):
                try:
                    if not self.authorized():return self.reply(403,{'error':'denied'})
                    if self.path=='/bridge/next':return self.reply(200,owner.next())
                    self.reply(404,{'error':'not_found'})
                except Exception:self.reply(400,{'error':'request_failed'})
            def do_POST(self):
                try:
                    if not self.authorized():return self.reply(403,{'error':'denied'})
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=524288 or self.headers.get('Transfer-Encoding') or self.headers.get_content_type()!='application/json':raise ValueError()
                    self.connection.settimeout(5)
                    body=json.loads(self.rfile.read(size))
                    if self.path=='/bridge/page': result=owner.accept(body)
                    elif self.path=='/bridge/finish':result=owner.finish(body)
                    else:return self.reply(404,{'error':'not_found'})
                    self.reply(200,result)
                except Exception:self.reply(400,{'error':'invalid_source_or_expired_job'})
        try:self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        except OSError:
            # Another UI/worker on this account can service the same durable queue.
            return False
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return True

    def close(self):
        if self.server:
            self.server.shutdown();self.server.server_close();self.thread.join(timeout=10)
