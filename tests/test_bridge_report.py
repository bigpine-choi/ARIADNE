from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen, Request
from urllib.error import HTTPError

from ariadne.automation_store import AutomationStore, now
from ariadne.browser_bridge import BrowserBridge
from ariadne.meeting_report import write_meeting_report
from ariadne.reporting import KST, write_report
from ariadne.weekly_archive import archive_weekly, archive_files
from test_calendar_archive import capture


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.store=AutomationStore(self.base/'archive.sqlite3');self.bridge=BrowserBridge(self.store,self.base)
        self.patch=patch.object(self.bridge,'token',return_value='synthetic-bridge-token');self.patch.start()
        self.bridge.start(port=0);self.url='http://127.0.0.1:'+str(self.bridge.server.server_port)
    def tearDown(self):self.bridge.close();self.patch.stop();self.store.close();self.tmp.cleanup()
    def test_untrusted_origins_and_missing_pairing_cannot_read_queue(self):
        for headers in ({},{'Origin':'https://evil.invalid','X-Ariadne-Bridge':'synthetic-bridge-token'},
          {'Origin':'chrome-extension://'+'a'*32}):
            with self.assertRaises(HTTPError) as error:urlopen(Request(self.url+'/bridge/next',headers=headers))
            self.assertEqual(403,error.exception.code)
    def test_explicit_extension_pairing_can_only_read_its_bridge(self):
        headers={'Origin':'chrome-extension://'+'a'*32,'X-Ariadne-Bridge':'synthetic-bridge-token'}
        with urlopen(Request(self.url+'/bridge/next',headers=headers)) as response:
            self.assertEqual({'job':None},json.load(response))
        with self.assertRaises(HTTPError) as error:urlopen(Request(self.url+'/api/settings/openai',headers=headers))
        self.assertEqual(404,error.exception.code)
    def test_archive_transaction_and_duplicate_capture_keep_one_version(self):
        deadline=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
        self.store.connection.execute("INSERT INTO browser_jobs VALUES('job','example.invalid','running',?,?,'{\"documents\":0,\"versions\":0}')",(now(),deadline))
        self.assertEqual(1,self.bridge.accept({'jobId':'job','page':capture()})['stored'])
        self.assertEqual(0,self.bridge.accept({'jobId':'job','page':capture()})['stored'])
        result=self.bridge.finish({'jobId':'job','complete':False,'warnings':['document_limit_reached']})
        self.assertEqual(1,result['versions']);self.assertFalse(result['complete'])
        with self.assertRaises(ValueError):self.bridge.accept({'jobId':'job','page':capture()})
    def test_expired_job_never_accepts_document(self):
        deadline=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        self.store.connection.execute("INSERT INTO browser_jobs VALUES('job','example.invalid','running',?,?,'{}')",(now(),deadline))
        with self.assertRaises(ValueError):self.bridge.accept({'jobId':'job','page':capture()})
        self.assertEqual([],self.store.history('hiworks:example.invalid'))


class MeetingLayoutTests(unittest.TestCase):
    def test_long_korean_summary_keeps_visible_appendix_notice(self):
        from ariadne.meeting_report import meeting_summary
        value=meeting_summary('가나다라'*1000)
        self.assertEqual(20,len(value.splitlines()))
        self.assertIn('주간업무보고 시트 참조',value.splitlines()[-1])

    def test_source_workbook_auxiliary_formulas_preserved_without_modifying_original(self):
        from openpyxl import Workbook, load_workbook
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);template=base/'template.xlsx';output=base/'generated.xlsx'
            book=Workbook();sheet=book.active;sheet.title='주간회의'
            sheet['A1']='주간업무회의록';sheet.merge_cells('A1:D1');sheet['A4']='펀드'
            sheet['C3']='금주';sheet['D3']='차주';sheet['C4']='old content';sheet['D4']='old plan'
            for name in ['공모주','운용','Sheet1','Sheet2','계약정리']:
                other=book.create_sheet(name);other['A1']='Historical reference';other['B2']='=1+2'
            book.save(template);original=template.read_bytes()
            start=datetime(2026,9,25,16,tzinfo=KST);end=start+timedelta(days=7)
            report={'items':[{'project':'Synthetic Fund','work':'Review','result':'Checked evidence','issues':'Pending','next_week':'Confirm date','category':'fund_management','evidence_ids':['S1']}]}
            sources=[{'source_id':'one','provider':'synthetic','body':'synthetic','evidence_id':'S1'}]
            write_report(output,report,sources,start,end,['teams_screen_scope_only'])
            write_meeting_report(output,template,report,sources,start,end,['teams_screen_scope_only'])
            actual=load_workbook(output)
            try:
                self.assertEqual(datetime(2026,10,2),actual['주간회의']['D2'].value)
                self.assertNotEqual('old content',actual['주간회의']['C4'].value)
                self.assertIn('Synthetic Fund',actual['주간회의']['C6'].value)
                self.assertIn('Confirm date',actual['주간회의']['D6'].value)
                for name in ['공모주','운용','Sheet1','Sheet2','계약정리']:self.assertEqual('=1+2',actual[name]['B2'].value)
                self.assertIn('근거',actual.sheetnames)
                self.assertEqual(original,template.read_bytes())
            finally:actual.close()

    def test_weekly_manifest_preserves_exact_old_version_after_later_file_update(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);folder=base/'source';folder.mkdir();file=folder/'meeting.txt';file.write_bytes(b'first')
            store=AutomationStore(base/'archive.sqlite3')
            try:
                settings={'weekly_archive_enabled':True,'folder':str(folder)}
                result=archive_weekly(store,settings,datetime(2026,10,2,16,tzinfo=KST))
                file.write_bytes(b'later');store.sync_folder(folder)
                manifest=archive_files(store,result['id'])
                self.assertEqual(b'first',store.read_version(manifest[0]['id']))
                self.assertEqual('already_archived',archive_weekly(store,settings,datetime(2026,10,2,17,tzinfo=KST))['status'])
            finally:store.close()
