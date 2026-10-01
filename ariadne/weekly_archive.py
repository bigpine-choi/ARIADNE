"""Immutable Friday 16:00 KST file-version manifests, independent of AI and mail."""
from datetime import datetime, timezone
import hashlib
import json

from .automation_store import canonical, now
from .reporting import report_window


def initialize(store):
    with store.lock:
        store.connection.executescript('''CREATE TABLE IF NOT EXISTS weekly_archive_runs(
        id TEXT PRIMARY KEY,scope TEXT NOT NULL,week_end TEXT NOT NULL,status TEXT NOT NULL,
        observed_at TEXT NOT NULL,detail TEXT NOT NULL,attempts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS weekly_archive_files(
        run_id TEXT NOT NULL REFERENCES weekly_archive_runs(id),
        source_id TEXT NOT NULL,version_id INTEGER NOT NULL REFERENCES archive_versions(id),
        PRIMARY KEY(run_id,source_id));''')


def archive_weekly(store, settings, at=None, *, force=False):
    initialize(store)
    if not settings.get('weekly_archive_enabled') and not force:
        return {'status':'disabled'}
    if not settings.get('folder'):
        raise ValueError('A source folder is required')
    at = at or datetime.now(timezone.utc)
    _, end = report_window(at)
    if not force and (at-end).total_seconds() > 3*86400:
        return {'status':'not_due'}
    scope = 'folder:' + canonical(settings['folder'])
    identifier = f'{end:%Y-%m-%d}-' + hashlib.sha256(scope.encode()).hexdigest()[:12]
    with store.transaction() as db:
        previous = db.execute('SELECT * FROM weekly_archive_runs WHERE id=?',(identifier,)).fetchone()
        if previous and (previous['status'] != 'failed' or previous['attempts'] >= 3):
            return {'id':identifier,'status':'already_archived' if previous['status']=='completed' else previous['status']}
        db.execute("""INSERT INTO weekly_archive_runs VALUES(?,?,?,'collecting',?,'{}',1)
          ON CONFLICT(id) DO UPDATE SET status='collecting',observed_at=excluded.observed_at,
          attempts=attempts+1,detail='{}'""",(identifier,scope,end.isoformat(),now()))
    try:
        summary = store.sync_folder(settings['folder'])
        with store.transaction() as db:
            latest = store.latest(scope)
            files = [(identifier,key,row['id']) for key,row in latest.items() if not row['deleted']]
            db.executemany('INSERT INTO weekly_archive_files VALUES(?,?,?)',files)
            detail = {'files':len(files),'scan':summary,'catchUp':at > end,
                      'note':'실제 읽은 시점의 보관본입니다. 종료 시각 당시 원본을 소급 복원하지 않습니다.'}
            db.execute("UPDATE weekly_archive_runs SET status='completed',observed_at=?,detail=? WHERE id=?",
                       (now(),json.dumps(detail,ensure_ascii=False),identifier))
        return {'id':identifier,'status':'completed',**detail}
    except Exception as error:
        with store.transaction() as db:
            db.execute("UPDATE weekly_archive_runs SET status='failed',detail=? WHERE id=?",
                       (json.dumps({'errorType':type(error).__name__}),identifier))
        raise


def archive_runs(store):
    initialize(store)
    with store.lock:
        rows = store.connection.execute('SELECT * FROM weekly_archive_runs ORDER BY week_end DESC').fetchall()
    return [{**dict(r),'detail':json.loads(r['detail'])} for r in rows]


def archive_files(store, identifier):
    initialize(store)
    with store.lock:
        rows = store.connection.execute('''SELECT v.id,v.source_id,v.version,v.hash,v.observed_at
          FROM weekly_archive_files f JOIN archive_versions v ON v.id=f.version_id
          WHERE f.run_id=? ORDER BY f.source_id''',(identifier,)).fetchall()
    return [dict(r) for r in rows]
