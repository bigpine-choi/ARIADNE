"""Encrypted, ordered folder-version delivery through an owner-selected sync folder.

Only a folder scope is exported. Each PC owns its SQLite files; neither opens a
database on OneDrive. Published packets are immutable and retained for recovery.
"""
from datetime import datetime
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from .automation_store import canonical, now
from .credentials import _crypt
from .weekly_archive import initialize as initialize_weekly

MAGIC = b'ARIADNE-HANDOFF-1\0'
MAX_BYTES = 512 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024


def initialize(store):
    initialize_weekly(store)
    with store.lock:
        store.connection.executescript('''
        CREATE TABLE IF NOT EXISTS handoff_stream(id INTEGER PRIMARY KEY CHECK(id=1),stream TEXT NOT NULL,scope TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS handoff_outbox(sequence INTEGER PRIMARY KEY,packet_id TEXT NOT NULL,payload BLOB,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS handoff_exported(version_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS handoff_imports(sequence INTEGER PRIMARY KEY,stream TEXT NOT NULL,packet_id TEXT NOT NULL,digest TEXT NOT NULL,source_at TEXT NOT NULL,received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS handoff_versions(remote_id INTEGER PRIMARY KEY,local_id INTEGER NOT NULL REFERENCES archive_versions(id));
        ''')


def install_key(directory, key):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError('Invalid handoff key')
    path = Path(directory) / 'credentials/shared-folder.dpapi'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if read_key(directory) != key:
            raise ValueError('A different handoff key is already registered')
        return
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(_crypt(key)); temporary.replace(path)


def read_key(directory):
    path = Path(directory) / 'credentials/shared-folder.dpapi'
    key = _crypt(path.read_bytes(), decrypt=True)
    if len(key) != 32: raise ValueError('Invalid handoff key')
    return key


def seal(payload, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, payload, MAGIC)


def unseal(payload, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if payload[:len(MAGIC)] != MAGIC or not 44 < len(payload) <= MAX_BYTES:
        raise ValueError('Invalid handoff packet')
    return AESGCM(key).decrypt(payload[len(MAGIC):len(MAGIC)+12], payload[len(MAGIC)+12:], MAGIC)


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 1000 or any(c in value for c in '\\:\0<>|?*"'):
        raise ValueError('Invalid relative file path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in {'.','..'} or p.endswith((' ','.')) for p in path.parts):
        raise ValueError('Invalid relative file path')
    if any(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', p.split('.')[0], re.I) for p in path.parts):
        raise ValueError('Reserved file path')
    return value


def timestamp(value):
    if not isinstance(value, str): raise ValueError('Invalid source time')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None: raise ValueError('Source time requires timezone')
    return stamp


def _write_packet(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.partial', delete=False) as handle:
        temporary = Path(handle.name); handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    try: temporary.replace(path)
    finally: temporary.unlink(missing_ok=True)


def export_folder(store, folder, destination, key):
    initialize(store)
    scope = 'folder:' + canonical(folder)
    destination = Path(destination).resolve(strict=True)
    if not destination.is_dir(): raise ValueError('Sync folder is unavailable')
    if destination.is_relative_to(Path(folder).resolve()): raise ValueError('Sync folder must be outside the source')
    with store.transaction() as db:
        identity = db.execute('SELECT * FROM handoff_stream').fetchone()
        if identity and identity['scope'] != scope: raise ValueError('Relay source changed; use a separate archive')
        if identity is None:
            db.execute('INSERT INTO handoff_stream VALUES(1,?,?)', (str(uuid4()),scope))
    # Recover exact sealed bytes after a publication failure, before exporting more.
    with store.lock:
        pending = store.connection.execute("SELECT * FROM handoff_outbox WHERE status='pending' ORDER BY sequence").fetchall()
        stream = store.connection.execute('SELECT stream FROM handoff_stream').fetchone()['stream']
    for row in pending:
        _write_packet(destination / f'{stream}-{row["sequence"]:010d}.ariadne-packet', bytes(row['payload']))
        with store.transaction() as db:
            db.execute("UPDATE handoff_outbox SET status='published',payload=NULL WHERE sequence=?", (row['sequence'],))
    with store.transaction() as db:
        scan = db.execute('SELECT completed_at FROM archive_scans WHERE scope=? ORDER BY completed_at DESC LIMIT 1', (scope,)).fetchone()
        if scan is None: raise ValueError('Complete the folder scan before delivery')
        rows = db.execute('''SELECT v.* FROM archive_versions v LEFT JOIN handoff_exported e ON e.version_id=v.id
            WHERE v.scope=? AND e.version_id IS NULL ORDER BY v.id''', (scope,)).fetchall()
        sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM handoff_outbox').fetchone()[0]
        packet_id = str(uuid4())
        current = [{'source_id':k,'remote_id':r['id']} for k,r in store.latest(scope).items() if not r['deleted']]
        weeks = []
        for row in db.execute("SELECT * FROM weekly_archive_runs WHERE scope=? AND status='completed'", (scope,)).fetchall():
            files = [{'source_id':r['source_id'],'remote_id':r['version_id']} for r in db.execute('SELECT * FROM weekly_archive_files WHERE run_id=?', (row['id'],))]
            weeks.append({'id':row['id'],'week_end':row['week_end'],'observed_at':row['observed_at'],'detail':json.loads(row['detail']),'files':files})
        versions = [{**dict(r),'metadata':json.loads(r['metadata']),'deleted':bool(r['deleted'])} for r in rows]
        for row in versions: row['remote_id']=row.pop('id'); row.pop('scope')
        manifest = {'schema':1,'stream':stream,'sequence':sequence,'packet_id':packet_id,
                    'scope':scope,'source_at':scan['completed_at'],'versions':versions,'current':current,'weeks':weeks}
        raw = BytesIO(); total = 0; seen = set()
        with ZipFile(raw,'w',ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
            for row in rows:
                if row['deleted'] or row['hash'] in seen: continue
                content = store.read_version(row['id']); total += len(content); seen.add(row['hash'])
                if len(content)>MAX_FILE or total>MAX_BYTES-4*1024*1024: raise ValueError('Delivery exceeds size limit')
                archive.writestr('blobs/'+row['hash'],content)
        payload = seal(raw.getvalue(),key)
        if len(payload)>MAX_BYTES: raise ValueError('Delivery exceeds size limit')
        db.execute("INSERT INTO handoff_outbox VALUES(?,?,?,'pending')",(sequence,packet_id,payload))
        db.executemany('INSERT INTO handoff_exported VALUES(?)',[(r['id'],) for r in rows])
    path = destination / f'{stream}-{sequence:010d}.ariadne-packet'
    _write_packet(path,payload)
    with store.transaction() as db:
        db.execute("UPDATE handoff_outbox SET status='published',payload=NULL WHERE sequence=?",(sequence,))
    return {'status':'published','sequence':sequence,'versions':len(rows),'files':len(current),'sourceAt':manifest['source_at']}


def _decode(payload, key):
    with ZipFile(BytesIO(unseal(payload,key))) as archive:
        names = archive.namelist()
        if len(names)!=len(set(names)) or 'manifest.json' not in names or len(names)>10001:
            raise ValueError('Invalid packet entries')
        if archive.getinfo('manifest.json').file_size>4*1024*1024 or sum(i.file_size for i in archive.infolist())>MAX_BYTES:
            raise ValueError('Expanded packet exceeds limit')
        manifest = json.loads(archive.read('manifest.json'))
        expected={'schema','stream','sequence','packet_id','scope','source_at','versions','current','weeks'}
        if not isinstance(manifest,dict) or set(manifest)!=expected or manifest['schema']!=1:
            raise ValueError('Invalid packet manifest')
        for name in ['stream','packet_id']:
            if str(__import__('uuid').UUID(manifest[name]))!=manifest[name]: raise ValueError('Invalid packet identity')
        if type(manifest['sequence']) is not int or manifest['sequence']<1 or not isinstance(manifest['scope'],str) or not manifest['scope'].startswith('folder:'):
            raise ValueError('Invalid packet sequence or scope')
        source_time=timestamp(manifest['source_at'])
        for field in ['versions','current','weeks']:
            if not isinstance(manifest[field],list) or len(manifest[field])>10000: raise ValueError('Invalid packet list')
        blobs = {}; identifiers = set(); allowed={'remote_id','source_id','version','hash','metadata','observed_at','deleted'}
        for row in manifest['versions']:
            if not isinstance(row,dict) or set(row)!=allowed or type(row['remote_id']) is not int or row['remote_id']<1 or row['remote_id'] in identifiers:
                raise ValueError('Invalid source version')
            identifiers.add(row['remote_id']); relative_path(row['source_id'])
            if timestamp(row['observed_at'])>source_time:raise ValueError('Version observed after its source scan')
            if type(row['version']) is not int or row['version']<1 or type(row['deleted']) is not bool or not isinstance(row['metadata'],dict): raise ValueError('Invalid source metadata')
            if set(row['metadata'])-{'relative_path','size','modified_ns'} or row['metadata'].get('relative_path')!=row['source_id']:
                raise ValueError('Only folder metadata may be delivered')
            for field in ['size','modified_ns']:
                if field in row['metadata'] and (type(row['metadata'][field]) is not int or row['metadata'][field]<0):
                    raise ValueError('Invalid file metadata')
            if row['deleted']:
                if row['hash'] is not None: raise ValueError('Deleted version contains a blob')
                continue
            digest = row['hash']
            if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest): raise ValueError('Invalid source hash')
            name = 'blobs/'+digest
            if name not in names or archive.getinfo(name).file_size>MAX_FILE: raise ValueError('Source blob missing or oversized')
            if digest not in blobs:
                content=archive.read(name)
                if hashlib.sha256(content).hexdigest()!=digest: raise ValueError('Source integrity mismatch')
                blobs[digest]=content
            if row['metadata'].get('size')!=len(blobs[digest]):raise ValueError('Source size mismatch')
        if set(names)!={'manifest.json',*('blobs/'+h for h in blobs)}: raise ValueError('Unexpected packet data')
        def validate_references(rows):
            if len(rows)>10000:raise ValueError('Too many file references')
            seen=set()
            for row in rows:
                if not isinstance(row,dict) or set(row)!={'source_id','remote_id'} or type(row['remote_id']) is not int or row['remote_id']<1:
                    raise ValueError('Invalid version reference')
                relative_path(row['source_id'])
                if row['source_id'] in seen: raise ValueError('Duplicate version reference')
                seen.add(row['source_id'])
        validate_references(manifest['current'])
        for week in manifest['weeks']:
            if not isinstance(week,dict) or set(week)!={'id','week_end','observed_at','detail','files'} or not isinstance(week['id'],str) or len(week['id'])>100:
                raise ValueError('Invalid weekly archive')
            timestamp(week['week_end']);timestamp(week['observed_at'])
            if not isinstance(week['detail'],dict) or not isinstance(week['files'],list): raise ValueError('Invalid weekly archive detail')
            validate_references(week['files'])
        return manifest, blobs


def import_packet(store, path, cache, key):
    initialize(store)
    path=Path(path)
    if path.stat().st_size>MAX_BYTES: raise ValueError('Packet exceeds size limit')
    payload=path.read_bytes(); digest=hashlib.sha256(payload).hexdigest(); manifest,blobs=_decode(payload,key)
    cache=Path(cache).resolve(); scope='folder:'+canonical(cache)
    with store.transaction() as db:
        prior=db.execute('SELECT * FROM handoff_imports ORDER BY sequence DESC LIMIT 1').fetchone()
        previous=db.execute('SELECT * FROM handoff_imports WHERE sequence=?',(manifest['sequence'],)).fetchone()
        if previous:
            if previous['digest']!=digest or previous['stream']!=manifest['stream']: raise ValueError('Packet sequence conflict')
            return {'status':'already_imported','sequence':manifest['sequence']}
        if manifest['sequence'] != (prior['sequence']+1 if prior else 1) or prior and prior['stream']!=manifest['stream']:
            raise ValueError('Delivery has a missing packet or different sender')
        if prior and timestamp(manifest['source_at'])<timestamp(prior['source_at']): raise ValueError('Source time moved backwards')
        for row in manifest['versions']:
            if db.execute('SELECT 1 FROM handoff_versions WHERE remote_id=?',(row['remote_id'],)).fetchone(): raise ValueError('Version already assigned')
            old=store.latest(scope).get(row['source_id'])
            if row['version'] != (old['version']+1 if old else 1): raise ValueError('Source version history has a gap')
            content=None if row['deleted'] else blobs[row['hash']]
            metadata={**row['metadata'],'origin_scope':manifest['scope'],'origin_observed_at':row['observed_at'],'origin_version':row['version']}
            store._append(db,scope,row['source_id'],content,metadata,row['version'],row['deleted'])
            identifier=db.execute('SELECT last_insert_rowid()').fetchone()[0]
            db.execute('INSERT INTO handoff_versions VALUES(?,?)',(row['remote_id'],identifier))
        def local(reference):
            row=db.execute('''SELECT v.* FROM handoff_versions h JOIN archive_versions v ON v.id=h.local_id WHERE h.remote_id=?''',(reference['remote_id'],)).fetchone()
            if row is None or row['source_id']!=reference['source_id'] or row['deleted']: raise ValueError('Referenced file version is unavailable')
            return row
        current={r['source_id']:local(r) for r in manifest['current']}
        actual={k:r for k,r in store.latest(scope).items() if not r['deleted']}
        if set(current)!=set(actual) or any(current[k]['id']!=actual[k]['id'] for k in current): raise ValueError('Current file manifest does not match version history')
        for week in manifest['weeks']:
            run_id='remote:'+manifest['stream']+':'+week['id']
            references=[local(r) for r in week['files']]
            existing=db.execute('SELECT * FROM weekly_archive_runs WHERE id=?',(run_id,)).fetchone()
            if existing:
                stored={r['source_id']:r['version_id'] for r in db.execute('SELECT * FROM weekly_archive_files WHERE run_id=?',(run_id,))}
                if stored!={r['source_id']:r['id'] for r in references} or existing['week_end']!=week['week_end']:
                    raise ValueError('Immutable weekly archive changed')
            else:
                detail={**week['detail'],'imported':True,'origin_scope':manifest['scope']}
                db.execute("INSERT INTO weekly_archive_runs VALUES(?,?,?,'completed',?,?,1)",(run_id,scope,week['week_end'],week['observed_at'],json.dumps(detail,ensure_ascii=False)))
                db.executemany('INSERT INTO weekly_archive_files VALUES(?,?,?)',[(run_id,r['source_id'],r['id']) for r in references])
        # Cache only managed file paths, for the workbook template. No source writes.
        cache.mkdir(parents=True,exist_ok=True)
        for identifier,row in current.items():
            target=(cache/relative_path(identifier)).resolve()
            if not target.is_relative_to(cache): raise ValueError('Cache path escaped its root')
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=row['hash']:
                _write_packet(target,store.read_version(row['id']))
        for identifier,row in store.latest(scope).items():
            if row['deleted']:
                target=(cache/relative_path(identifier)).resolve()
                if not target.is_relative_to(cache): raise ValueError('Cache path escaped its root')
                target.unlink(missing_ok=True)
        db.execute('INSERT INTO archive_scans VALUES(?,?,?,?)',(str(uuid4()),scope,now(),json.dumps({'files':len(current),'imported':True})))
        db.execute('INSERT INTO handoff_imports VALUES(?,?,?,?,?,?)',(manifest['sequence'],manifest['stream'],manifest['packet_id'],digest,manifest['source_at'],now()))
    return {'status':'imported','sequence':manifest['sequence'],'versions':len(manifest['versions']),'files':len(current),'sourceAt':manifest['source_at']}


def import_available(store, directory, cache, key):
    directory=Path(directory).resolve(strict=True)
    if not directory.is_dir(): raise ValueError('Sync folder unavailable')
    initialize(store)
    with store.lock:
        prior=store.connection.execute('SELECT * FROM handoff_imports ORDER BY sequence DESC LIMIT 1').fetchone()
    results=[]
    for path in sorted(directory.glob('*.ariadne-packet')):
        if path.is_symlink() or not path.resolve().is_relative_to(directory): raise ValueError('Packet path escaped sync folder')
        identity=re.fullmatch(r'([0-9a-f-]{36})-(\d{10})\.ariadne-packet',path.name)
        if not identity:raise ValueError('Invalid packet filename')
        if prior and identity[1]==prior['stream'] and int(identity[2])<=prior['sequence']:continue
        results.append(import_packet(store,path,cache,key))
    return {'status':'imported' if results else 'waiting','packets':sum(r['status']=='imported' for r in results),**status(store)}


def status(store):
    initialize(store)
    with store.lock:
        row=store.connection.execute('SELECT source_at,received_at,sequence FROM handoff_imports ORDER BY sequence DESC LIMIT 1').fetchone()
        out=store.connection.execute('SELECT sequence,status FROM handoff_outbox ORDER BY sequence DESC LIMIT 1').fetchone()
    return {'lastReceived':dict(row) if row else None,'lastPublished':dict(out) if out else None}
