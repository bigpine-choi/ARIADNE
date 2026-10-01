"""Versioned source archive and durable weekly runs, separate from domain schema 1.

The archive stores actual bytes, not links to mutable shared originals. Never put
this database in a source checkout or on an SMB share.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


class AutomationStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.lock = RLock()
        self.connection.execute('PRAGMA busy_timeout=10000')
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS archive_blobs(hash TEXT PRIMARY KEY, content BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS archive_versions(
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, source_id TEXT NOT NULL,
          version INTEGER NOT NULL, hash TEXT REFERENCES archive_blobs(hash),
          metadata TEXT NOT NULL, observed_at TEXT NOT NULL, deleted INTEGER NOT NULL,
          UNIQUE(scope, source_id, version));
        CREATE INDEX IF NOT EXISTS archive_source ON archive_versions(scope,source_id,version);
        CREATE TABLE IF NOT EXISTS archive_scans(id TEXT PRIMARY KEY,scope TEXT NOT NULL,
          completed_at TEXT NOT NULL, summary TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS weekly_runs(id TEXT PRIMARY KEY,start TEXT NOT NULL,
          end TEXT NOT NULL,status TEXT NOT NULL,output TEXT,detail TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0);
        ''')

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield self.connection
                self.connection.execute('COMMIT')
            except BaseException:
                self.connection.execute('ROLLBACK')
                raise

    def latest(self, scope):
        with self.lock:
            rows = self.connection.execute('''SELECT v.* FROM archive_versions v
            JOIN (SELECT source_id,MAX(version) version FROM archive_versions
              WHERE scope=? GROUP BY source_id) x
            ON v.source_id=x.source_id AND v.version=x.version WHERE v.scope=?''', (scope,scope))
            return {r['source_id']: dict(r) for r in rows}

    @staticmethod
    def _append(db, scope, identifier, content, metadata, version, deleted=False):
        digest = hashlib.sha256(content).hexdigest() if content is not None else None
        if content is not None:
            db.execute('INSERT OR IGNORE INTO archive_blobs VALUES(?,?)',(digest, content))
        db.execute('''INSERT INTO archive_versions(scope,source_id,version,hash,metadata,
        observed_at,deleted) VALUES(?,?,?,?,?,?,?)''',
        (scope,identifier,version,digest,json.dumps(metadata,ensure_ascii=False),now(),int(deleted)))

    def sync_folder(self, root, max_file_bytes=64*1024*1024, max_scan_bytes=512*1024*1024):
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError('Source must be a directory')
        if Path(self.connection.execute('PRAGMA database_list').fetchone()[2]).resolve().is_relative_to(root):
            raise ValueError('Archive database must be outside the source folder')
        scope = 'folder:' + canonical(root)
        with self.transaction() as db:
            records = {}
            total = 0
            # os.walk raises on unreadable directories, unlike a silently partial rglob.
            def fail(error):
                raise error
            for directory, folders, files in os.walk(root, followlinks=False, onerror=fail):
                folders[:] = [n for n in folders if not (Path(directory)/n).is_symlink()
                              and not getattr((Path(directory)/n),'is_junction',lambda:False)()]
                for filename in files:
                    path = Path(directory)/filename
                    if path.is_symlink() or filename.startswith('~$'):
                        continue
                    if not path.resolve().is_relative_to(root):
                        raise ValueError('Source path escaped its configured root')
                    before = path.stat()
                    if before.st_size > max_file_bytes:
                        raise ValueError('Source file exceeds size limit; previous snapshot retained')
                    with path.open('rb') as handle:
                        content = handle.read(max_file_bytes + 1)
                    after = path.stat()
                    if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns) or len(content)!=after.st_size:
                        raise RuntimeError('Source changed during reading; retry the scan')
                    total += len(content)
                    if len(content)>max_file_bytes or total>max_scan_bytes:
                        raise ValueError('Scan exceeds size limit; previous snapshot retained')
                    identifier = path.relative_to(root).as_posix()
                    records[identifier] = (content,{'relative_path':identifier,'size':len(content),
                      'modified_ns':after.st_mtime_ns})
            previous = self.latest(scope)
            summary = {'new':0,'updated':0,'unchanged':0,'deleted':0,'files':len(records)}
            for identifier,(content,metadata) in records.items():
                old = previous.get(identifier)
                digest = hashlib.sha256(content).hexdigest()
                if old and not old['deleted'] and old['hash']==digest:
                    summary['unchanged'] += 1
                    continue
                summary['updated' if old else 'new'] += 1
                self._append(db,scope,identifier,content,metadata,old['version']+1 if old else 1)
            for identifier,old in previous.items():
                if identifier not in records and not old['deleted']:
                    self._append(db,scope,identifier,None,json.loads(old['metadata']),old['version']+1,True)
                    summary['deleted'] += 1
            db.execute('INSERT INTO archive_scans VALUES(?,?,?,?)',(str(uuid4()),scope,now(),json.dumps(summary)))
        return summary

    def ingest_messages(self, scope, messages):
        with self.transaction() as db:
            previous = self.latest(scope)
            count = 0
            for message in messages:
                identifier = message['source_id']
                normalized={key:value for key,value in message.items() if key!='captured_at'}
                content = json.dumps(normalized,ensure_ascii=False,sort_keys=True).encode('utf-8')
                old = previous.get(identifier)
                digest = hashlib.sha256(content).hexdigest()
                if old and old['hash']==digest:
                    continue
                self._append(db,scope,identifier,content,{'kind':'message'},old['version']+1 if old else 1)
                previous[identifier]={'version':old['version']+1 if old else 1,'hash':digest}
                count += 1
        return count

    def archive_bytes(self, scope, identifier, content, metadata):
        with self.transaction() as db:
            previous=self.latest(scope).get(identifier)
            digest=hashlib.sha256(content).hexdigest()
            if not previous or previous['hash']!=digest:
                self._append(db,scope,identifier,content,metadata,previous['version']+1 if previous else 1)
            return self.latest(scope)[identifier]['id']

    def history(self, scope):
        with self.lock:
            return [dict(r) for r in self.connection.execute('''SELECT id,source_id,version,hash,
              metadata,observed_at,deleted FROM archive_versions WHERE scope=? ORDER BY id DESC''',(scope,))]

    def read_version(self, identifier):
        with self.lock:
            row = self.connection.execute('''SELECT b.hash,b.content FROM archive_versions v
              JOIN archive_blobs b ON b.hash=v.hash WHERE v.id=?''',(identifier,)).fetchone()
        if row is None:
            raise LookupError('Version not found or deleted')
        content = bytes(row['content'])
        if hashlib.sha256(content).hexdigest()!=row['hash']:
            raise RuntimeError('Archive content integrity check failed')
        return content

    def claim_run(self, identifier, start, end):
        with self.transaction() as db:
            row = db.execute('SELECT status,attempts FROM weekly_runs WHERE id=?',(identifier,)).fetchone()
            if row and (row['status']!='failed' or row['attempts']>=3):
                return False
            db.execute('''INSERT INTO weekly_runs(id,start,end,status,output,detail,attempts)
              VALUES(?,?,?,'collecting',NULL,'{}',1)
              ON CONFLICT(id) DO UPDATE SET status='collecting',detail='{}',attempts=attempts+1 ''',(identifier,start,end))
        return True

    def release_failed_collection(self, identifier):
        """Explicit local recovery after examining a dead collector; never resend.

        No release of sending/delivery_unknown/submitted is allowed here.
        """
        with self.transaction() as db:
            row=db.execute('SELECT status FROM weekly_runs WHERE id=?',(identifier,)).fetchone()
            if row is None or row['status'] not in {'collecting','failed','prepared'}:
                raise ValueError('Only a stopped collection or unsent report may be released')
            db.execute("UPDATE weekly_runs SET status='failed',attempts=0,detail=? WHERE id=?",
              (json.dumps({'recovery':'explicit_local_release'}),identifier))

    def set_run(self, identifier, status, output=None, detail=None):
        with self.transaction() as db:
            db.execute('UPDATE weekly_runs SET status=?,output=?,detail=? WHERE id=?',
              (status,str(output) if output else None,json.dumps(detail or {},ensure_ascii=False),identifier))

    def runs(self):
        with self.lock:
            return [dict(r) for r in self.connection.execute('SELECT * FROM weekly_runs ORDER BY end DESC')]

