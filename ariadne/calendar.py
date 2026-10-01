"""Source-linked calendar candidates; source refresh and AI never overwrite user reviews."""
import calendar as month_calendar
from contextlib import closing
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from .models import ValidationError
from .repository import ConflictError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def text(value):
    if value is None: return ''
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValidationError('일정표 셀 값의 형식을 확인하세요.')
    result = str(value).strip()
    if len(result) > 12000: raise ValidationError('일정표 셀 내용이 너무 깁니다.')
    return result


def month_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', value):
        raise ValidationError('조회 월은 YYYY-MM로 입력하세요.')
    return map(int, value.split('-'))


def dates_for(raw, year, month):
    """Only compute explicit calendar dates. Never invent business-day or conditional deadlines."""
    raw = raw.strip()
    end = month_calendar.monthrange(year, month)[1]
    if re.fullmatch(r'20\d{2}-\d{2}-\d{2}', raw):
        try: stamp = date.fromisoformat(raw)
        except ValueError: return [None]
        return [stamp.isoformat()] if (stamp.year, stamp.month) == (year, month) else []
    explicit = re.match(r"(?:(20\d{2})|(\d{2}))년\s*(\d{1,2})월\s*(\d{1,2})일", raw)
    if explicit:
        y = int(explicit[1] or ('20' + explicit[2])); m, d = int(explicit[3]), int(explicit[4])
        if (y, m) != (year, month): return []
        try: return [date(y, m, d).isoformat()]
        except ValueError: return [None]
    if re.fullmatch(r'(?:매월 )?말일', raw): return [date(year, month, end).isoformat()]
    if raw in {'매월 말 금요일', '월말 금요일'}:
        day = end - (date(year, month, end).weekday()-4) % 7
        return [date(year, month, day).isoformat()]
    if raw in {'매주 금', '매주 금요일'}:
        return [date(year, month, day).isoformat() for day in range(1, end+1) if date(year, month, day).weekday() == 4]
    # Month-specific overrides and annotations require explicit review, not silent precedence.
    if re.fullmatch(r'\d{1,2}일', raw):
        day = int(raw[:-1])
        return [date(year, month, day).isoformat()] if 1 <= day <= end else [None]
    if re.fullmatch(r'\d{1,2}일/\d{1,2}일', raw):
        return [date(year, month, int(day)).isoformat() if 1<=int(day)<=end else None for day in re.findall(r'(\d+)일', raw)]
    if raw == '초·20·25일': return [None, date(year, month, 20).isoformat(), date(year, month, 25).isoformat()]
    return [None]


def parse_snapshot(snapshot):
    required = {'schemaVersion', 'sourceId', 'title', 'capturedAt', 'modifiedAt', 'sourceTimezone', 'sheets'}
    if not isinstance(snapshot, dict) or set(snapshot) != required or type(snapshot['schemaVersion']) is not int or snapshot['schemaVersion'] != 1:
        raise ValidationError('일정표 가져오기 파일 형식을 확인하세요.')
    source = snapshot['sourceId']
    if not isinstance(source, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', source):
        raise ValidationError('원본 문서 ID를 확인하세요.')
    for name in ('title', 'capturedAt', 'modifiedAt', 'sourceTimezone'): text(snapshot[name])
    try:
        captured = datetime.fromisoformat(snapshot['capturedAt'])
        if captured.tzinfo is None: raise ValueError()
    except (TypeError, ValueError): raise ValidationError('자료 확인 시각을 확인하세요.') from None
    if not isinstance(snapshot['sheets'], list) or len(snapshot['sheets']) > 30:
        raise ValidationError('시트 범위를 확인하세요.')
    records, seen_sheets, count = [], set(), 0
    for sheet in snapshot['sheets']:
        if not isinstance(sheet, dict) or set(sheet) != {'id', 'title', 'layout', 'rows'}:
            raise ValidationError('시트 형식을 확인하세요.')
        if type(sheet['id']) is not int or sheet['id'] < 0 or sheet['id'] in seen_sheets:
            raise ValidationError('중복된 시트 ID입니다.')
        seen_sheets.add(sheet['id'])
        layout = sheet['layout']; title = text(sheet['title'])
        if layout not in {'annual', 'routine', 'setup', 'checklist', 'overview', 'empty'} or not isinstance(sheet['rows'], list):
            raise ValidationError('지원하지 않는 일정표 배치입니다.')
        fund_match = re.search(r'(\d+호)', title)
        fund = fund_match[1] if fund_match else '기타'
        mode, role, company, person = '', '', '', ''
        days = {}; previous_row = 0
        for entry in sheet['rows']:
            count += 1
            if count > 4000 or not isinstance(entry, dict) or set(entry) != {'row', 'cells'}:
                raise ValidationError('가져오기 범위를 확인하세요.')
            row = entry['row']; values = entry['cells']
            if type(row) is not int or row <= previous_row or not isinstance(values, list) or len(values) > 30:
                raise ValidationError('셀 좌표를 확인하세요.')
            previous_row = row
            cells = [text(v) for v in values]
            cells += [''] * (30-len(cells))
            def add(kind, label, timing='', months=None, description='', contact=None, column='A'):
                if not label: return
                # Parse whitelisted business columns only; never persist credential metadata.
                if re.search(r'(?i)(password|비밀번호|인증서\s*PW|API\s*키)', label + '\n' + description): return
                cell = f'{column}{row}'
                records.append({'id': digest([source, sheet['id'], row, column, kind]), 'kind': kind, 'fund': fund,
                    'title': label, 'timing': timing, 'months': months or [], 'description': description,
                    'contact': contact, 'sourceId': source, 'sheetTitle': title, 'cell': cell,
                    'sourceUrl': f'https://docs.google.com/spreadsheets/d/{source}/edit#gid={sheet["id"]}&range={quote(cell)}'})
            if layout == 'annual':
                heading = re.search(r'(\d+호).*Contact Point', cells[1], re.I)
                if heading or cells[1] == '케이에이고덕':
                    fund = heading[1] if heading else '기타'; mode = 'contacts'; role = company = person = ''; continue
                if cells[:4] == ['펀드', '업무', '구분', '기준일']: mode = 'annual'; continue
                if cells[0].startswith('상시'): mode = 'notes'; continue
                if mode == 'annual' and re.fullmatch(r'\d+호|기타', cells[0]):
                    fund = cells[0]
                    add('schedule', cells[1], cells[3], cells[4:16], '\n'.join(v for v in cells[16:] if v))
                elif mode == 'notes':
                    for col in (0, 2):
                        if re.fullmatch(r'\d+호|기타', cells[col]):
                            fund = cells[col]; add('note', cells[col+1], column='A' if col == 0 else 'C')
                elif mode == 'contacts' and cells[1] != '구분' and any(cells[3:8]):
                    if cells[1] and cells[1] != role: company = person = ''
                    if cells[2] and cells[2] != company: person = ''
                    role = cells[1] or role; company = cells[2] or company; person = cells[3] or person
                    add('contact', person or '공용 연락채널', description=cells[7], column='B',
                        contact={'role': role, 'company': company, 'name': person, 'position': cells[4],
                                 'phone': cells[5], 'email': cells[6], 'inactive': '퇴사' in cells[7]})
            elif layout == 'routine':
                if cells[1].startswith('2.'): mode = 'routine'; continue
                if cells[1].startswith(('3.', '4.')): mode = 'notes'; continue
                if cells[1] == '구분' or not cells[1]: continue
                add('note', cells[1], cells[3], description='\n'.join(v for v in cells[2:6] if v), column='B')
            elif layout == 'setup':
                for col in (range(1, 15, 2) if row <= 32 else []):
                    if re.fullmatch(r'20\d{2}-\d{2}-\d{2}', cells[col]):
                        days[col] = cells[col]; continue
                    if col in days and cells[col] and cells[col] not in {'대상', 'Mon', 'Tue', 'Wed', 'Thr', 'fri', 'Sat', 'Sun'}:
                        add('dated', cells[col], days[col], description='원본 완료 표기: ' + cells[col+1], column=chr(65+col))
                if 35 <= row <= 46 and cells[3] and cells[1] != '역할':
                    role = cells[1] or role; company = cells[2] or company
                    add('contact', cells[3], description=cells[6], column='B', contact={'role':role,'company':company,
                        'name':cells[3],'position':'','phone':cells[4],'email':cells[5],'inactive':'퇴사' in cells[6]})
            elif layout == 'checklist' and cells[1].isdigit():
                fund = '펀드 설정'; add('checklist', cells[2], description=cells[3]+'\n관련 기관: '+cells[4], column='C')
            elif layout == 'overview' and cells[1] and not cells[1].startswith('  -'):
                add('note', cells[1], description='\n'.join(v for v in cells[7:] if v), column='B')
    # A retirement note on any source for a fund/email remains visible on duplicates.
    retired = {(r['fund'], r['contact']['email'].strip().casefold()) for r in records
               if r['kind']=='contact' and r['contact']['inactive'] and r['contact']['email'].strip()}
    for record in records:
        if record['kind']=='contact' and (record['fund'], record['contact']['email'].strip().casefold()) in retired:
            record['contact']['inactive'] = True
    if not records: raise ValidationError('일정·연락처·운용 메모를 찾지 못했습니다.')
    return records


class CalendarStore:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS calendar_sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calendar_records(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,payload TEXT NOT NULL,version INTEGER NOT NULL,missing INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS calendar_reviews(id TEXT PRIMARY KEY,record_id TEXT NOT NULL,month TEXT NOT NULL,payload TEXT NOT NULL,version INTEGER NOT NULL,source_version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS calendar_audit(id INTEGER PRIMARY KEY,at TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL);''')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        return db

    def import_snapshot(self, snapshot):
        records = parse_snapshot(snapshot)
        source = {k:v for k,v in snapshot.items() if k != 'sheets'}
        source['coverage'] = [{'id':s['id'],'title':s['title'],'rows':len(s['rows'])} for s in snapshot['sheets']]
        with closing(self._db()) as db, db:
            previous = {r['id']:r for r in db.execute('SELECT * FROM calendar_records WHERE source_id=?', (source['sourceId'],))}
            db.execute('INSERT OR REPLACE INTO calendar_sources VALUES(?,?)', (source['sourceId'], encode(source)))
            counts = {'new':0,'changed':0,'unchanged':0,'missing':0}
            incoming = set()
            for record in records:
                identifier = record['id']; incoming.add(identifier); payload = encode(record); old = previous.get(identifier)
                version = old['version'] if old else 1
                if old and old['payload'] != payload: version += 1; counts['changed'] += 1
                elif old: counts['unchanged'] += 1
                else: counts['new'] += 1
                db.execute('INSERT OR REPLACE INTO calendar_records VALUES(?,?,?,?,0)', (identifier,source['sourceId'],payload,version))
            for identifier in set(previous)-incoming:
                if json.loads(previous[identifier]['payload'])['kind'] == 'ai': continue
                db.execute('UPDATE calendar_records SET missing=1 WHERE id=?', (identifier,)); counts['missing'] += 1
            db.execute('INSERT INTO calendar_audit(at,action,payload) VALUES(?,?,?)', (snapshot['capturedAt'],'source_import',encode(counts)))
        return counts

    def records(self):
        with closing(self._db()) as db:
            return [{**json.loads(r['payload']),'version':r['version'],'missingLatest':bool(r['missing'])}
                    for r in db.execute('SELECT * FROM calendar_records ORDER BY source_id,id')]

    def view(self, month):
        year, number = month_key(month)
        records = self.records(); events = []; extras = []
        with closing(self._db()) as db:
            reviews = {r['id']:r for r in db.execute('SELECT * FROM calendar_reviews WHERE month=?', (month,))}
            sources = [json.loads(r['payload']) for r in db.execute('SELECT * FROM calendar_sources')]
        indexed = {r['id']:r for r in records}
        for record in records:
            if record['kind'] not in {'schedule','dated','ai'}: extras.append(record); continue
            raw = record['months'][number-1] if record['months'] else record['timing']
            if not raw: continue
            slots = dates_for(raw, year, number)
            for slot, candidate in enumerate(slots):
                identifier = digest([record['id'],month,slot]); review = reviews.pop(identifier, None)
                event = {**record,'id':identifier,'recordId':record['id'],'month':month,'slot':slot,
                         'date':candidate,'status':'proposed','reviewVersion':0,'rawTiming':raw,
                         'sourceChanged':record['missingLatest'] or (record['kind']=='ai' and
                            (record.get('aiSourceVersion') != indexed.get(record.get('aiSourceId'),{}).get('version')
                             or indexed.get(record.get('aiSourceId'),{}).get('missingLatest', True)))}
                if review:
                    event.update(json.loads(review['payload'])); event['reviewVersion'] = review['version']
                    event['sourceChanged'] = event['sourceChanged'] or review['source_version'] != record['version']
                if event.get('status') != 'dismissed': events.append(event)
        # Retain reviewed occurrences if source timing or month membership changes.
        indexed = {r['id']:r for r in records}
        for identifier, review in reviews.items():
            record = indexed.get(review['record_id'])
            if record:
                event = {**record,**json.loads(review['payload']),'id':identifier,'recordId':record['id'],
                         'month':month,'reviewVersion':review['version'],'sourceChanged':True,'rawTiming':record['timing']}
                if event['status'] != 'dismissed': events.append(event)
        return {'month':month,'timezone':'Asia/Seoul','sources':sources,'events':events,'reference':extras,
                'funds':sorted({r['fund'] for r in records})}

    def review(self, value):
        keys = {'id','recordId','month','date','title','status','note','expectedVersion','sourceVersion'}
        if not isinstance(value,dict) or set(value) != keys: raise ValidationError('일정 검토 항목을 확인하세요.')
        if any(not isinstance(value[k],str) for k in ('id','recordId','month','title','status','note')):
            raise ValidationError('일정 검토 설명을 확인하세요.')
        month_key(value['month'])
        if value['status'] not in {'confirmed','completed','dismissed'}: raise ValidationError('일정 상태를 확인하세요.')
        if value['date'] is not None:
            try: parsed = date.fromisoformat(value['date'])
            except (ValueError,TypeError): raise ValidationError('일정 날짜를 확인하세요.') from None
            if parsed.strftime('%Y-%m') != value['month']: raise ValidationError('선택한 월의 날짜를 입력하세요.')
        elif value['status'] != 'dismissed': raise ValidationError('확정할 날짜를 입력하세요.')
        if not text(value['title']) or len(text(value['title'])) > 300 or len(text(value['note'])) > 8000:
            raise ValidationError('일정 제목·메모를 확인하세요.')
        if type(value['expectedVersion']) is not int or type(value['sourceVersion']) is not int:
            raise ValidationError('일정 버전을 확인하세요.')
        view = self.view(value['month']); allowed = {e['id']:e for e in view['events']}
        if value['id'] not in allowed or allowed[value['id']]['recordId'] != value['recordId']:
            raise ValidationError('현재 표시된 일정에서 검토하세요.')
        payload = {k:value[k] for k in ('date','title','status','note')}
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            source = db.execute('SELECT version FROM calendar_records WHERE id=?',(value['recordId'],)).fetchone()
            old = db.execute('SELECT version FROM calendar_reviews WHERE id=?',(value['id'],)).fetchone()
            if source['version'] != value['sourceVersion'] or (old['version'] if old else 0) != value['expectedVersion']:
                raise ConflictError('일정 또는 원본이 변경되었습니다. 새로고침 후 다시 검토하세요.')
            version = value['expectedVersion']+1
            db.execute('INSERT OR REPLACE INTO calendar_reviews VALUES(?,?,?,?,?,?)',
                       (value['id'],value['recordId'],value['month'],encode(payload),version,value['sourceVersion']))
            db.execute('INSERT INTO calendar_audit(at,action,payload) VALUES(?,?,?)',
                       (datetime.now().astimezone().isoformat(),'user_review',encode({'id':value['id'],**payload})))
        return {'saved':True,'version':version}

    def save_ai(self, proposals, selected):
        if not isinstance(proposals,dict) or set(proposals) != {'items'} or not isinstance(proposals['items'],list) or len(proposals['items']) > 100:
            raise ValidationError('AI 일정 응답 형식이 올바르지 않습니다.')
        records = []
        for proposal in proposals['items']:
            if not isinstance(proposal,dict) or set(proposal) != {'sourceId','date','title','note'} or not isinstance(proposal['sourceId'],str) or proposal['sourceId'] not in selected:
                raise ValidationError('AI가 없는 근거를 사용했습니다.')
            record = selected[proposal['sourceId']]
            if proposal['date'] is not None:
                try: date.fromisoformat(proposal['date'])
                except (ValueError,TypeError): raise ValidationError('AI 일정 날짜가 올바르지 않습니다.') from None
            if not text(proposal['title']) or len(text(proposal['title']))>300 or len(text(proposal['note']))>8000:
                raise ValidationError('AI 일정 설명이 올바르지 않습니다.')
            records.append({**record,'id':digest(['ai',proposal,record['version']]),'kind':'ai',
                            'title':proposal['title'],'description':proposal['note'],'timing':proposal['date'] or '확인 필요',
                            'months':[],'aiSourceId':record['id'],'aiSourceVersion':record['version']})
        with closing(self._db()) as db, db:
            for record in records:
                for key in ('version','missingLatest'): record.pop(key,None)
                db.execute('INSERT OR IGNORE INTO calendar_records(id,source_id,payload,version) VALUES(?,?,?,1)',
                           (record['id'],record['sourceId'],encode(record)))
        return {'created':len(records)}
