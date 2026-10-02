"""Real local run state; native dependencies mocked, no external actions."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from ariadne.automation_store import AutomationStore
from ariadne.local_settings import DEFAULTS
from ariadne.native_powershell import NativeLaunchError
from ariadne.reporting import KST
from ariadne import weekly_worker as worker


class NativeSendPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        environment = patch.dict(os.environ, {'ARIADNE_DATA_DIR':str(self.base)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = AutomationStore(self.base/'archive.sqlite3')
        self.addCleanup(self.store.close)
        self.settings = {**DEFAULTS,'mailbox':'owner@example.test','enabled':True,
                         'auto_send':False,'teams_chats':[],'model':'synthetic'}
        owner = self.settings['mailbox']
        suffix = hashlib.sha256(owner.encode()).hexdigest()[:12]
        self.identifier = '2026-10-02-'+suffix+'-validation-'+str(uuid4())
        self.output = self.base/'reports'/'weekly-test.xlsx'
        self.output.parent.mkdir()
        self.content = b'synthetic-approved-report'
        self.output.write_bytes(self.content)
        self.digest = hashlib.sha256(self.content).hexdigest()
        version = self.store.archive_bytes('reports:'+owner,self.output.name,self.content,
                                           {'kind':'generated_report'})
        self.store.claim_run(self.identifier,'2026-09-25T07:00:00Z','2026-10-02T07:00:00Z')
        self.store.set_run(self.identifier,'prepared',self.output,
            {'recipient':owner,'kind':'operational_validation','outputHash':self.digest,
             'outputVersionId':version})

    def row(self):
        row = next(row for row in self.store.runs() if row['id']==self.identifier)
        return {**row,'detail':json.loads(row['detail'])}

    def test_known_prelaunch_failure_keeps_prepared_and_does_not_call_sender(self):
        for code in ('CLASSIC_OUTLOOK_MISSING','POWERSHELL_POLICY_BLOCKED'):
            with self.subTest(code=code),patch.object(worker.local_mail,'preflight',
                    side_effect=NativeLaunchError(code)) as preflight, \
                    patch.object(worker.local_mail,'send_to_self') as sender:
                with self.assertRaisesRegex(NativeLaunchError,code):
                    worker.send_prepared(self.store,self.settings,self.identifier,self.digest)
                preflight.assert_called_once_with()
                sender.assert_not_called()
                self.assertEqual('prepared',self.row()['status'])
                self.assertNotIn('sendingStartedAt',self.row()['detail'])
                self.assertNotIn('approvedAt',self.row()['detail'])

    def test_injected_sender_is_unchanged_and_skips_host_preflight(self):
        sender = Mock(return_value={'submitted':True,'delivered':False})
        with patch.object(worker.local_mail,'preflight',side_effect=AssertionError('Host access forbidden')) as preflight:
            result = worker.send_prepared(self.store,self.settings,self.identifier,self.digest,sender=sender)
        preflight.assert_not_called()
        sender.assert_called_once()
        self.assertEqual('submitted',result['status'])

    def test_default_sender_preflight_succeeds_then_dispatches_once(self):
        with patch.object(worker.local_mail,'preflight',return_value={'ready':True}) as preflight, \
                patch.object(worker.local_mail,'send_to_self',return_value={'submitted':True,'delivered':False}) as sender:
            self.assertEqual('submitted',worker.send_prepared(self.store,self.settings,self.identifier,self.digest)['status'])
        preflight.assert_called_once_with()
        sender.assert_called_once()

    def test_error_after_dispatch_starts_preserves_unknown_state_and_no_retry(self):
        with patch.object(worker.local_mail,'preflight',return_value={'ready':True}), \
                patch.object(worker.local_mail,'send_to_self',side_effect=NativeLaunchError('OUTLOOK_COM_UNAVAILABLE')) as sender:
            self.assertEqual('delivery_unknown',worker.send_prepared(self.store,self.settings,self.identifier,self.digest)['status'])
            with self.assertRaises(ValueError):
                worker.send_prepared(self.store,self.settings,self.identifier,self.digest)
        sender.assert_called_once()
        self.assertEqual('delivery_unknown',self.row()['status'])

    def test_auto_send_prelaunch_failure_keeps_generated_report_downloadable(self):
        message = {'source_id':'synthetic','provider':'synthetic','body':'Evidence',
                   'timestamp':'2026-10-01T02:00:00Z','subject':'Synthetic activity','direction':'sent'}
        report = {'items':[{'project':'Synthetic Fund','work':'Review','result':'Checked',
                  'issues':'','next_week':'Confirm','evidence_ids':['S1']}]}
        extractor = Mock(return_value=(report,[{**message,'evidence_id':'S1'}]))
        with patch.object(worker.local_mail,'preflight',side_effect=NativeLaunchError('CLASSIC_OUTLOOK_MISSING')), \
                patch.object(worker.local_mail,'send_to_self') as sender:
            result = worker.run_weekly(self.store,{**self.settings,'auto_send':True},
                datetime(2026,10,2,16,tzinfo=KST),
                mail_collector=lambda *args:{'messages':[message],'complete':True,'warnings':[]},
                teams_collector=lambda *args:{'messages':[],'complete':True,'warnings':[]},
                extractor=extractor)
        self.assertEqual('prepared',result['status'])
        self.assertEqual('CLASSIC_OUTLOOK_MISSING',result['nativePreflightCode'])
        self.assertTrue(Path(result['output']).is_file())
        sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
