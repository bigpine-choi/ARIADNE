from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ariadne.calendar import CalendarStore, dates_for
from ariadne.calendar_ai import propose
from ariadne.hiworks_archive import archive_detail, safe_url
from ariadne.automation_store import AutomationStore
from ariadne.models import ValidationError
from ariadne.repository import ConflictError
from ariadne.reporting import KST, write_report
from ariadne.teams_uia import normalize


def snapshot():
    return {'schemaVersion':1,'sourceId':'synthetic-sheet','title':'Synthetic schedules',
      'capturedAt':'2026-10-01T03:00:00+00:00','modifiedAt':'2026-09-30','sourceTimezone':'America/Los_Angeles',
      'sheets':[{'id':1,'title':'Annual','layout':'annual','rows':[
        {'row':1,'cells':['펀드','업무','구분','기준일']},
        {'row':2,'cells':['1호','Synthetic monthly task','정기','말일']+['말일']*12+['근거 메모']},
        {'row':3,'cells':['1호','Conditional task','조건부','입금 후']+['입금 후']*12},
        {'row':4,'cells':['','1호 Contact Point']},
        {'row':5,'cells':['','PM','Synthetic Co','Person','PM','000','nobody@example.invalid','퇴사']}
      ]}]}


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=CalendarStore(Path(self.tmp.name)/'calendar.sqlite3')
        self.store.import_snapshot(snapshot())
    def tearDown(self):self.tmp.cleanup()
    def test_month_end_and_last_friday_keep_korean_date(self):
        self.assertEqual(['2026-10-31'],dates_for('말일',2026,10))
        self.assertEqual(['2026-10-30'],dates_for('월말 금요일',2026,10))
        self.assertIn('2026-10-31',[e['date'] for e in self.store.view('2026-10')['events']])
    def test_ambiguous_conditions_and_invalid_dates_are_not_invented(self):
        for raw in ['15일 전후','입금 후','30일(8/28)','2영업일','2026-02-30']:
            self.assertEqual([None],dates_for(raw,2026,2))
        self.assertEqual([],dates_for('27년 6월 30일',2026,6))
    def test_reviews_survive_source_refresh_and_require_new_version(self):
        event=next(e for e in self.store.view('2026-10')['events'] if e['date'])
        review={k:event[k] for k in ['id','recordId','month','date','title']}
        review.update(status='confirmed',note='Human confirmed',expectedVersion=0,sourceVersion=event['version'])
        self.store.review(review)
        revised=snapshot();revised['sheets'][0]['rows'][1]['cells'][1]='Changed source task'
        self.store.import_snapshot(revised)
        actual=next(e for e in self.store.view('2026-10')['events'] if e['id']==event['id'])
        self.assertEqual('Human confirmed',actual['note']);self.assertEqual(event['title'],actual['title'])
        self.assertTrue(actual['sourceChanged'])
        with self.assertRaises(ConflictError):self.store.review(review)
    def test_missing_source_retains_review(self):
        event=next(e for e in self.store.view('2026-10')['events'] if e['date'])
        self.store.review({**{k:event[k] for k in ['id','recordId','month','date','title']},'status':'confirmed','note':'Keep','expectedVersion':0,'sourceVersion':1})
        revised=snapshot();revised['sheets'][0]['rows'].pop(1);self.store.import_snapshot(revised)
        actual=next(e for e in self.store.view('2026-10')['events'] if e['id']==event['id'])
        self.assertTrue(actual['sourceChanged']);self.assertEqual('Keep',actual['note'])
    def test_credentials_are_not_calendar_records(self):
        revised=snapshot();revised['sheets'][0]['rows'][1]['cells'][1]='비밀번호: synthetic-do-not-store'
        self.store.import_snapshot(revised)
        self.assertNotIn('synthetic-do-not-store',json.dumps(self.store.records()))
    def test_contacts_have_retirement_and_provenance(self):
        contact=self.store.view('2026-10')['reference'][0]
        self.assertTrue(contact['contact']['inactive']);self.assertIn('range=B5',contact['sourceUrl'])
    def test_ai_atomic_invalid_source_and_date_rejection(self):
        record=next(r for r in self.store.records() if r['kind']=='schedule')
        items=[{'sourceId':record['id'],'title':'Allowed','date':'2026-10-02','note':'Source'},
               {'sourceId':'missing','title':'Invented','date':None,'note':''}]
        with self.assertRaises(ValidationError):self.store.save_ai({'items':items},{record['id']:record})
        self.assertFalse(any(r['kind']=='ai' for r in self.store.records()))
    def test_ai_does_not_receive_contacts_and_invalid_month_does_not_save(self):
        record=next(r for r in self.store.records() if r['kind']=='schedule');seen=[]
        def opener(request,**kwargs):
            seen.append(json.loads(request.data))
            reply={'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps({'items':[{'sourceId':record['id'],'date':'2026-11-01','title':'Wrong month','note':''}]})}]}]}
            return io.BytesIO(json.dumps(reply).encode())
        with patch('ariadne.calendar_ai.resolve_openai_key',return_value='synthetic-key'):
            with self.assertRaises(ValidationError):propose(self.store,'2026-10',[record['id']],'synthetic-model',opener=opener)
        self.assertNotIn('nobody@example.invalid',json.dumps(seen));self.assertFalse(seen[0]['store'])
        self.assertFalse(any(r['kind']=='ai' for r in self.store.records()))


def capture():
    return {'kind':'detail','url':'https://approval.office.hiworks.com/example.invalid/approval/document/view/123',
      'documentId':'123','company':'example.invalid','documentNumber':'SYNTHETIC-1','title':'Synthetic approval',
      'metadata':{'문서 번호':'SYNTHETIC-1','기안 일시':'2026-10-01 09:10:00','기안자':'Synthetic Owner'},
      'body':'Synthetic content','approvals':[{'text':'Approval pending','marks':[]}],
      'history':['Synthetic submit 2026-10-01 09:10:00'],'historyLoaded':True,'attachments':[],
      'capturedAt':'2026-10-01T03:00:00+00:00'}


class HiworksArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=AutomationStore(Path(self.tmp.name)/'archive.sqlite3')
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_readonly_scope_blocks_write_other_company_and_wrong_document(self):
        for url in ['https://approval.office.hiworks.com/example.invalid/approval/document/write','https://evil.invalid/example.invalid/approval/document/view/123','https://approval.office.hiworks.com/other.invalid/approval/document/view/123']:
            with self.assertRaises(ValidationError):safe_url(url,'example.invalid',detail=True)
        page=capture();page['documentId']='999'
        with self.assertRaises(ValidationError):archive_detail(self.store,page,'example.invalid')
        self.assertEqual([],self.store.history('hiworks:example.invalid'))
    def test_capture_time_does_not_create_versions_but_history_change_does(self):
        page=capture();self.assertEqual(1,archive_detail(self.store,page,'example.invalid')['stored'])
        page['capturedAt']='2026-10-01T04:00:00+00:00'
        self.assertEqual(0,archive_detail(self.store,page,'example.invalid')['stored'])
        page['history'].append('Synthetic approval updated')
        self.assertEqual(1,archive_detail(self.store,page,'example.invalid')['stored'])
        self.assertEqual(2,len(self.store.history('hiworks:example.invalid')))
    def test_approval_completion_never_asserts_financial_execution(self):
        page=capture();page['metadata']['완료 일시']='2026-10-01 10:00:00'
        archive_detail(self.store,page,'example.invalid')
        message=json.loads(self.store.read_version(self.store.history('hiworks:example.invalid')[0]['id']))
        self.assertEqual('completion_time_present',message['source_status'])
        self.assertNotIn('executionComplete',message)
    def test_extra_fields_and_cross_document_attachments_rejected(self):
        page=capture();page['cookie']='synthetic'
        with self.assertRaises(ValidationError):archive_detail(self.store,page,'example.invalid')
        page=capture();page['attachments']=[{'name':'file.xlsx','url':'https://approval-storage-api.office.hiworks.com/download/documents/999/files/1'}]
        with self.assertRaises(ValidationError):archive_detail(self.store,page,'example.invalid')


class TeamsNormalizationTests(unittest.TestCase):
    def test_absolute_dates_scope_and_unknown_date(self):
        start=datetime(2026,10,1,9,tzinfo=KST);end=start+timedelta(hours=9)
        rows=[{'id':'message-body-1','chat':'allowed','text':'Synthetic 2026년 10월 1일 목요일 오후 12:01 hello'},
              {'id':'message-body-2','chat':'allowed','text':'날짜 미확인 Synthetic'}]
        messages,unknown=normalize(rows,['allowed'],start,end)
        self.assertEqual(1,unknown);self.assertEqual('2026-10-01T12:01:00+09:00',messages[0]['timestamp'])
        self.assertIsNone(messages[1]['timestamp'])
        with self.assertRaises(ValueError):normalize(rows,['other'],start,end)
    def test_out_of_period_excluded(self):
        start=datetime(2026,10,1,9,tzinfo=KST);end=start+timedelta(hours=9)
        rows=[{'id':'message-body-1','chat':'allowed','text':'Synthetic 2026년 9월 30일 수요일 오전 9:01 hello'}]
        self.assertEqual([],normalize(rows,['allowed'],start,end)[0])
