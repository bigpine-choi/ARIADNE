"""Source integrity, real report files, weekly boundaries and failure recovery."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from ariadne.automation_store import AutomationStore, canonical
from ariadne.local_settings import DEFAULTS, load_settings
from ariadne.local_teams import normalize_capture, keyboard_capture
from ariadne.reporting import KST, report_window, validate_report, write_report, extract_report
from ariadne.weekly_worker import run_weekly, scheduled_tick
from ariadne.repository import Repository
from ariadne.server import create_server


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.root=self.base/'source'; self.root.mkdir()
        self.store=AutomationStore(self.base/'archive.sqlite3')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_changed_same_size_same_mtime_is_detected_and_old_bytes_survive(self):
        file=self.root/'회의록.xlsx'; file.write_bytes(b'first')
        stamp=file.stat()
        self.assertEqual(1,self.store.sync_folder(self.root)['new'])
        first=self.store.history('folder:'+canonical(self.root))[0]
        file.write_bytes(b'other'); os.utime(file,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.assertEqual(1,self.store.sync_folder(self.root)['updated'])
        self.assertEqual(b'first',self.store.read_version(first['id']))
        self.assertEqual(1,self.store.sync_folder(self.root)['unchanged'])

    def test_delete_and_recreate_keep_history(self):
        file=self.root/'doc.pdf'; file.write_bytes(b'pdf')
        self.store.sync_folder(self.root)
        file.unlink(); self.assertEqual(1,self.store.sync_folder(self.root)['deleted'])
        self.assertEqual(0,self.store.sync_folder(self.root)['deleted'])
        file.write_bytes(b'pdf'); self.assertEqual(1,self.store.sync_folder(self.root)['updated'])
        versions=self.store.history('folder:'+canonical(self.root))
        self.assertEqual([3,2,1],[r['version'] for r in versions])
        self.assertEqual(1,self.store.connection.execute('SELECT COUNT(*) FROM archive_blobs').fetchone()[0])

    def test_partial_or_disconnected_scan_never_marks_deletion(self):
        (self.root/'doc.pdf').write_bytes(b'123')
        self.store.sync_folder(self.root)
        with self.assertRaises(ValueError):
            self.store.sync_folder(self.root,max_file_bytes=2)
        with self.assertRaises(FileNotFoundError):
            self.store.sync_folder(self.base/'unmounted')
        self.assertEqual(1,len(self.store.history('folder:'+canonical(self.root))))

    def test_read_failure_rolls_back_entire_scan(self):
        (self.root/'a.txt').write_bytes(b'a')
        with patch.object(Path,'open',side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError): self.store.sync_folder(self.root)
        self.assertEqual([],self.store.history('folder:'+canonical(self.root)))

    def test_source_cannot_contain_its_archive(self):
        inside=AutomationStore(self.root/'bad.sqlite3')
        try:
            with self.assertRaises(ValueError): inside.sync_folder(self.root)
        finally: inside.close()

    def test_message_versions_are_idempotent(self):
        m={'source_id':'one','body':'original'}
        self.assertEqual(1,self.store.ingest_messages('mail:a',[m,m]))
        self.assertEqual(0,self.store.ingest_messages('mail:a',[m]))
        self.assertEqual(1,self.store.ingest_messages('mail:a',[{**m,'body':'edited'}]))
        self.assertEqual(2,len(self.store.history('mail:a')))

    def test_capture_timestamp_does_not_create_fake_message_updates(self):
        m={'source_id':'snapshot','body':'text','captured_at':'one'}
        self.assertEqual(1,self.store.ingest_messages('teams:a',[m]))
        self.assertEqual(0,self.store.ingest_messages('teams:a',[{**m,'captured_at':'later'}]))


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'ARIADNE_DATA_DIR':str(self.base)}); self.env.start()
        self.store=AutomationStore(self.base/'archive.sqlite3')
        self.at=datetime(2026,10,2,16,0,tzinfo=KST)
        self.settings={**DEFAULTS,'mailbox':'owner@example.invalid','model':'configured-test-model',
          'enabled':True,'auto_send':True,'teams_chats':['synthetic-chat']}
        self.sources=[{'source_id':'email-1','provider':'outlook_com','direction':'sent',
          'timestamp':'2026-10-01T02:00:00Z','subject':'[가상] 자료 전달','body':'[가상] 계약 검토 자료를 전달했습니다.'}]
        self.report={'items':[{'project':'가상 펀드','work':'계약 검토','result':'자료 전달, 완료 확인 필요',
          'issues':'확인 필요','next_week':'확인 필요','evidence_ids':['S1']}]}
        self.sent=[]

    def tearDown(self):
        self.store.close(); self.env.stop(); self.tmp.cleanup()

    def execute(self,**overrides):
        dependencies={'mail_collector':lambda *a:{'messages':self.sources,'complete':True,'warnings':[]},
          'teams_collector':lambda *a:{'messages':[],'complete':True,'warnings':[]},
          'extractor':lambda *a:(self.report,[{**self.sources[0],'evidence_id':'S1'}]),
          'sender':lambda *a:self.sent.append(a) or {'submitted':True}}
        dependencies.update(overrides)
        return run_weekly(self.store,self.settings,self.at,**dependencies)

    def test_weekly_preparation_also_persists_hiworks_draft(self):
        from ariadne.hiworks import DraftStore
        self.settings['auto_send']=False
        result=self.execute()
        self.assertEqual('prepared',result['status'])
        draft=DraftStore(self.base/'hiworks.sqlite3').get(result['hiworksDraftId'])
        self.assertEqual('weekly',draft['kind'])
        self.assertEqual(['S1'],draft['evidenceIds'])
        self.assertFalse(draft['externalSubmission'])
        run=self.store.runs()[0]
        self.assertEqual(result['hiworksDraftId'],json.loads(run['detail'])['hiworksDraftId'])

    def test_friday_boundary_and_weekend_catchup(self):
        start,end=report_window(self.at)
        self.assertEqual(timedelta(days=7),end-start)
        self.assertEqual(end,report_window(self.at+timedelta(days=1))[1])
        self.assertEqual(end-timedelta(days=7),report_window(self.at-timedelta(seconds=1))[1])
        self.assertEqual(16,end.hour)

    def test_submitted_report_is_never_sent_twice(self):
        self.assertEqual('submitted',self.execute()['status'])
        self.assertEqual('already_claimed',self.execute()['status'])
        self.assertEqual(1,len(self.sent))
        self.assertTrue(Path(self.sent[0][1]).is_file())
        self.assertEqual(self.settings['mailbox'],self.sent[0][0])

    def test_incomplete_teams_never_calls_ai_or_mail(self):
        def fail(*args): raise AssertionError('must not call')
        result=self.execute(teams_collector=lambda *a:{'messages':[],'complete':False,'warnings':['unavailable']},
          extractor=fail,sender=fail)
        self.assertEqual('incomplete_sources',result['status'])
        self.assertEqual(1,len(self.store.history('outlook:'+self.settings['mailbox'])))

    def test_partial_mode_keeps_visible_warnings(self):
        self.settings['allow_partial_report']=True
        result=self.execute(teams_collector=lambda *a:normalize_capture('synthetic-chat','undated message'))
        self.assertEqual('submitted',result['status'])
        from openpyxl import load_workbook
        workbook=load_workbook(result['output'])
        self.assertIn('teams_collection_incomplete',workbook['수집 범위']['B4'].value)
        workbook.close()

    def test_unknown_send_outcome_is_not_retried(self):
        def sender(*args): raise TimeoutError('after submit')
        self.assertEqual('delivery_unknown',self.execute(sender=sender)['status'])
        self.assertEqual('already_claimed',self.execute()['status'])
        self.assertEqual(0,len(self.sent))

    def test_prepared_report_does_not_regenerate_every_tick(self):
        self.settings['auto_send']=False
        self.assertEqual('prepared',self.execute()['status'])
        self.assertEqual('already_claimed',self.execute()['status'])

    def test_failed_collection_has_bounded_retries(self):
        def failure(*args): raise RuntimeError('unavailable')
        for _ in range(3):
            with self.assertRaises(RuntimeError): self.execute(mail_collector=failure)
        self.assertEqual('already_claimed',self.execute()['status'])

    def test_disabled_scheduler_has_no_side_effect(self):
        self.settings['enabled']=False
        self.assertEqual('disabled',self.execute()['status'])
        self.assertEqual([],self.store.runs())

    def test_post_send_database_failure_cannot_enable_duplicate_send(self):
        original=self.store.set_run
        def persist(identifier,status,*args,**kwargs):
            if status=='submitted': raise OSError('disk unavailable')
            return original(identifier,status,*args,**kwargs)
        with patch.object(self.store,'set_run',side_effect=persist):
            with self.assertRaises(OSError): self.execute()
        self.assertEqual('delivery_unknown',self.store.runs()[0]['status'])
        self.assertEqual('already_claimed',self.execute()['status'])
        self.assertEqual(1,len(self.sent))

    def test_explicit_collection_recovery_allows_retry(self):
        self.settings['auto_send']=False
        result=self.execute()
        self.store.release_failed_collection(result['id'])
        self.assertEqual('prepared',self.execute()['status'])

    def test_sent_or_uncertain_runs_cannot_be_released(self):
        result=self.execute()
        for status in ('sending','delivery_unknown','submitted'):
            self.store.set_run(result['id'],status)
            with self.assertRaises(ValueError): self.store.release_failed_collection(result['id'])

    def test_wrong_period_message_fails_before_external_analysis(self):
        self.sources[0]['timestamp']='2020-01-01T00:00:00Z'
        with self.assertRaises(ValueError): self.execute()
        self.assertFalse(self.sent)

    def test_no_catchup_outside_three_day_window(self):
        self.assertEqual('not_due',scheduled_tick(self.store,self.settings,self.at+timedelta(days=4))['status'])


class ReportContractTests(unittest.TestCase):
    def test_fabricated_evidence_rejected(self):
        row={'project':'a','work':'b','result':'c','issues':'d','next_week':'e','evidence_ids':['missing']}
        with self.assertRaises(ValueError): validate_report({'items':[row]},{'S1'})

    def test_excel_text_is_never_a_formula(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as directory:
            row={'project':'=1+1','work':' @danger','result':'normal','issues':'\x00test','next_week':'+test','evidence_ids':['S1']}
            source={'source_id':'=bad','provider':'fake','body':'=WEBSERVICE("https://invalid")','evidence_id':'S1'}
            at=datetime(2026,10,2,16,tzinfo=KST)
            output=write_report(Path(directory)/'report.xlsx',{'items':[row]},[source],at-timedelta(days=7),at,[])
            workbook=load_workbook(output)
            self.assertTrue(all(c.data_type!='f' for ws in workbook for row in ws for c in row))
            self.assertEqual("'=1+1",workbook['주간업무보고']['A4'].value)
            workbook.close()

    def test_api_refusal_and_incomplete_are_rejected(self):
        import io
        messages=[{'source_id':'m','provider':'fake','body':'test'}]
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-key'}):
            for response in ({'status':'incomplete'}, {'status':'completed','output':[{'content':[{'type':'refusal'}]}]}):
                def opener(*a,**k): return io.BytesIO(json.dumps(response).encode())
                with self.assertRaises(RuntimeError): extract_report(messages,'test-model',opener=opener)

    def test_settings_reject_string_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text('{"auto_send":"false"}')
            with self.assertRaises(ValueError): load_settings(path)


class TeamsKeyboardTests(unittest.TestCase):
    class Driver:
        def __init__(self,texts): self.texts=iter(texts); self.actions=[]; self.verifications=0
        def verify(self,chat): self.verifications+=1
        def key(self,action): self.actions.append(action)
        def clipboard_sequence(self): return 1
        def read_copied(self,before): return next(self.texts,None)
        def preserve_clipboard(self):
            from contextlib import nullcontext
            return nullcontext()

    def test_bounded_copy_keeps_unknown_dates_and_never_types_or_sends(self):
        driver=self.Driver(['first','second','third'])
        result=keyboard_capture('chat',driver,limit=2)
        self.assertEqual(2,len(result['messages']))
        self.assertFalse(result['complete'])
        self.assertTrue(all(m['timestamp'] is None for m in result['messages']))
        self.assertEqual({'focus_messages','newest','copy','previous'},set(driver.actions))

    def test_stale_clipboard_is_not_imported(self):
        driver=self.Driver([None])
        with self.assertRaises(RuntimeError): keyboard_capture('chat',driver)

    def test_foreground_chat_change_aborts(self):
        driver=self.Driver(['text'])
        def verify(chat):
            driver.verifications+=1
            if driver.verifications>1: raise RuntimeError('switched')
        driver.verify=verify
        with self.assertRaises(RuntimeError): keyboard_capture('chat',driver)
        self.assertNotIn('copy',driver.actions)


class AutomationHttpTests(unittest.TestCase):
    def test_sync_button_and_original_version_download_with_origin_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); root=base/'source'; root.mkdir()
            (root/'weekly.txt').write_bytes(b'original')
            repository=Repository(base/'domain.sqlite3')
            archive=AutomationStore(base/'archive.sqlite3')
            server=create_server(repository,port=0,automation_store=archive,automation_settings={**DEFAULTS,'folder':str(root)})
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            url=f'http://127.0.0.1:{server.server_port}'
            try:
                request=Request(url+'/api/automation/sync',data=b'{}',headers={'Content-Type':'application/json'},method='POST')
                with urlopen(request) as response: self.assertEqual(1,json.load(response)['new'])
                request=Request(url+'/api/automation/sync',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://evil.invalid'},method='POST')
                with self.assertRaises(HTTPError) as error: urlopen(request)
                self.assertEqual(403,error.exception.code)
                with urlopen(url+'/api/automation/versions') as response: version=json.load(response)['items'][0]
                with urlopen(url+f'/api/automation/versions/{version["id"]}/download') as response:
                    self.assertEqual(b'original',response.read())
                with urlopen(url) as response: self.assertIn('Content-Security-Policy',response.headers)
                with urlopen(url+'/navigation.js') as response:
                    self.assertEqual('text/javascript', response.headers.get_content_type())
                    self.assertGreater(len(response.read()), 0)
            finally:
                server.shutdown(); server.server_close(); thread.join(); archive.close(); repository.close()

