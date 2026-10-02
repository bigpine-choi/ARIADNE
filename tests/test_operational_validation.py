"""Operational validation must use proven source versions and never send implicitly."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from ariadne.automation_store import AutomationStore, canonical, now
from ariadne.local_settings import DEFAULTS
from ariadne.models import ValidationError
from ariadne.reporting import KST, report_window
from ariadne.weekly_archive import archive_weekly, archive_files
from ariadne.workspace import Workspace


class OperationalValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.folder=self.base/'shared-meetings';self.folder.mkdir()
        self.template=self.folder/'meeting-20261002.xlsx';self.template.write_bytes(b'frozen-company-template')
        self.store=AutomationStore(self.base/'archive.sqlite3')
        self.friday=datetime(2026,10,2,16,tzinfo=KST);self.at=self.friday+timedelta(minutes=15)
        self.message={'source_id':'mail-1','provider':'outlook_com','timestamp':'2026-10-01T10:00:00+09:00',
                      'subject':'Synthetic request','body':'Synthetic current-period work','sender':'counterparty@example.invalid'}
        self.mail=Mock(return_value={'messages':[self.message],'complete':True,'warnings':[]})
        self.sender=Mock()
        self.ws=Workspace(self.store,self.base/'settings.json',Mock(),mail=self.mail,sender=self.sender)
        self.settings={**DEFAULTS,'device_role':'omen','handoff_required':True,'enabled':True,
                       'folder':str(self.folder),'mailbox':'owner@example.invalid',
                       'report_template':str(self.template),'model':'synthetic-model','auto_send':True}
        self.ws.save_settings(self.settings)
        self.archived=archive_weekly(self.store,{**self.settings,'device_role':'single'},self.friday,force=True)
        self.frozen=archive_files(self.store,self.archived['id'])
        self.ws._import_handoff=Mock(return_value={'status':'received'})
        self.ws._collect_mail(self.settings,self.friday)

    def tearDown(self):
        self.ws.close();self.store.close();self.tmp.cleanup()

    def capture(self,settings=None,**kwargs):
        with patch('ariadne.workspace.run_weekly',side_effect=lambda *args,**deps:(args,deps)):
            return self.ws.prepare_weekly(settings or self.settings,self.at,**kwargs)

    def test_validation_same_period_as_scheduled_and_never_autosends(self):
        scheduled,normal=self.capture()
        validated,validation=self.capture(validation=True,scope_acknowledged=True)
        self.assertEqual(report_window(scheduled[2]),report_window(validated[2]))
        self.assertEqual(self.friday,report_window(validated[2])[1])
        self.assertTrue(scheduled[1]['auto_send']);self.assertFalse(validated[1]['auto_send'])
        self.assertTrue(validation['strict_sources']);self.assertTrue(validation['scope_acknowledged'])
        self.assertEqual('',normal['identifier_suffix'])
        self.assertTrue(validation['identifier_suffix'].startswith('validation-'))
        self.sender.assert_not_called()

    def test_validation_does_not_consume_actual_scheduled_run_id(self):
        report={'items':[{'project':'Synthetic project','work':'Current-period review','result':'Reviewed',
                         'issues':'','next_week':'Follow up','evidence_ids':['S1']}]}
        self.ws.reporter=Mock(return_value=(report,[{**self.message,'evidence_id':'S1'}]))
        self.store.ingest_messages('outlook:'+self.settings['mailbox'],[{**self.message,'body':'updated after cutoff'}])
        latest_id=self.store.latest('outlook:'+self.settings['mailbox'])['mail-1']['id']
        def write(output,*args):
            output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'synthetic-reviewable-report')
        with patch('ariadne.weekly_worker.data_directory',return_value=self.base), \
                patch('ariadne.weekly_worker.write_report',side_effect=write), \
                patch('ariadne.meeting_report.write_meeting_report',side_effect=write), \
                patch('ariadne.source_text.extract_text',return_value='Synthetic meeting context'), \
                patch('ariadne.hiworks.DraftStore') as drafts:
            drafts.return_value.save.return_value={'id':'synthetic-draft'}
            validated=self.ws.prepare_weekly(self.settings,self.at,validation=True,scope_acknowledged=True)
            self.assertEqual('prepared',validated['status']);self.sender.assert_not_called()
            self.assertEqual(latest_id,self.store.latest('outlook:'+self.settings['mailbox'])['mail-1']['id'])
            official=self.ws.prepare_weekly({**self.settings,'auto_send':False,'allow_partial_report':True},self.at)
            self.assertEqual('prepared',official['status'])
            self.assertNotEqual(validated['id'],official['id'])
            self.assertNotIn('validation',official['id'])
            self.assertEqual(latest_id,self.store.latest('outlook:'+self.settings['mailbox'])['mail-1']['id'])
            duplicate=self.ws.prepare_weekly({**self.settings,'auto_send':False,'allow_partial_report':True},self.at)
            self.assertEqual('already_claimed',duplicate['status'])
        self.assertEqual(2,len(self.store.runs()));self.sender.assert_not_called()

    def test_scope_acknowledgement_and_partial_setting_cannot_bypass_critical_failure(self):
        self.mail.return_value={'messages':[self.message],'complete':False,'warnings':['mail_body_truncated']}
        self.ws._collect_mail(self.settings,self.friday)
        self.ws.reporter=Mock()
        with patch('ariadne.source_text.extract_text',return_value='Synthetic meeting context'):
            result=self.ws.prepare_weekly({**self.settings,'allow_partial_report':True},self.at,
                                         validation=True,scope_acknowledged=True)
        self.assertEqual('incomplete_sources',result['status'])
        self.assertIn('mail_body_truncated',result['warnings'])
        self.ws.reporter.assert_not_called();self.sender.assert_not_called()

    def test_each_validation_has_separate_identifier(self):
        _,one=self.capture(validation=True)
        _,two=self.capture(validation=True)
        self.assertNotEqual(one['identifier_suffix'],two['identifier_suffix'])
        self.assertNotIn('preview',one['identifier_suffix'])

    def test_preview_remains_a_separate_shifted_period(self):
        preview,_=self.capture(preview=True)
        validated,_=self.capture(validation=True)
        self.assertEqual(timedelta(days=7),report_window(preview[2])[1]-report_window(validated[2])[1])

    def test_validation_requires_omen_and_rejects_preview(self):
        with self.assertRaises(ValidationError):self.capture({**self.settings,'device_role':'single'},validation=True)
        with self.assertRaises(ValidationError):self.capture(validation=True,preview=True)

    def test_validation_requires_configured_company_template(self):
        with self.assertRaises(ValidationError):self.capture({**self.settings,'report_template':''},validation=True)

    def test_validation_requires_archive_even_when_handoff_required_disabled(self):
        with patch('ariadne.workspace.archive_runs',return_value=[]),patch('ariadne.workspace.run_weekly') as runner:
            with self.assertRaises(ValidationError):
                self.ws.prepare_weekly({**self.settings,'handoff_required':False},self.at,validation=True)
        runner.assert_not_called()

    def test_previous_week_archive_cannot_satisfy_validation(self):
        wrong={'id':'previous','scope':'folder:'+canonical(self.folder),'status':'completed',
               'week_end':(self.friday-timedelta(days=7)).isoformat()}
        with patch('ariadne.workspace.archive_runs',return_value=[wrong]):
            with self.assertRaises(ValidationError):self.capture(validation=True)

    def test_missing_template_in_frozen_snapshot_is_rejected(self):
        with patch('ariadne.weekly_archive.archive_files',return_value=[]):
            with self.assertRaises(ValidationError):self.capture(validation=True)

    def test_template_outside_company_folder_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.capture({**self.settings,'report_template':str(self.base/'outside.xlsx')},validation=True)

    def test_frozen_template_and_meeting_versions_survive_later_change(self):
        self.template.write_bytes(b'changed-after-cutoff');self.store.sync_folder(self.folder)
        _,deps=self.capture(validation=True)
        self.assertEqual(b'frozen-company-template',deps['template_bytes'])
        with patch('ariadne.source_text.extract_text',return_value='Synthetic meeting context') as extract:
            context,warnings,limitations=deps['extra_sources'](*report_window(self.at))
        self.assertEqual(self.frozen[0]['id'],context[0]['archiveVersionId'])
        self.assertEqual(b'frozen-company-template',extract.call_args.args[0])
        self.assertEqual([],warnings)

    def test_archive_only_without_collection_receipt_is_unverified(self):
        self.store.connection.execute("DELETE FROM archive_scans WHERE scope LIKE 'outlook:%'")
        _,deps=self.capture(validation=True,scope_acknowledged=True)
        result=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertFalse(result['complete']);self.assertIn('outlook_archive_collection_unverified',result['warnings'])

    def test_scope_limitations_are_separate_from_real_collection_failures(self):
        _,deps=self.capture(validation=True)
        result=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertTrue(result['complete']);self.assertEqual([],result['warnings'])
        self.assertIn('mail_archive_snapshot_only',result['limitations'])
        self.assertIn('mail_attachment_contents_not_collected',result['limitations'])
        receipt=json.loads(self.store.connection.execute(
            "SELECT summary FROM archive_scans WHERE scope LIKE 'outlook:%' ORDER BY rowid DESC").fetchone()['summary'])
        self.assertTrue(receipt['scopeComplete'])
        version=self.store.latest('outlook:'+self.settings['mailbox'])['mail-1']['id']
        self.assertEqual([version],receipt['versionIds'])

    def test_late_changed_mail_uses_immutable_collected_version(self):
        changed={**self.message,'body':'changed after Friday cutoff'}
        self.store.ingest_messages('outlook:'+self.settings['mailbox'],[changed])
        _,deps=self.capture(validation=True)
        collected=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertEqual(self.message['body'],collected['messages'][0]['body'])

    def test_truncation_failure_cannot_be_converted_to_scope_limitation(self):
        self.mail.return_value={'messages':[self.message],'complete':False,'warnings':['mail_body_truncated']}
        self.ws._collect_mail(self.settings,self.friday)
        _,deps=self.capture(validation=True,scope_acknowledged=True)
        collected=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertFalse(collected['complete']);self.assertIn('mail_body_truncated',collected['warnings'])
        self.assertNotIn('mail_body_truncated',collected['limitations'])

    def test_collection_exception_creates_failure_receipt(self):
        self.mail.side_effect=RuntimeError('synthetic unavailable')
        with self.assertRaises(RuntimeError):self.ws._collect_mail(self.settings,self.friday)
        _,deps=self.capture(validation=True,scope_acknowledged=True)
        collected=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertFalse(collected['complete']);self.assertIn('mail_collection_failed',collected['warnings'])

    def test_collection_that_misses_start_of_week_is_unverified(self):
        self.store.connection.execute("DELETE FROM archive_scans WHERE scope LIKE 'outlook:%'")
        self.ws._record_collection('outlook:'+self.settings['mailbox'],self.mail.return_value,
                                  self.friday-timedelta(days=7)+timedelta(minutes=1),self.friday+timedelta(minutes=1))
        _,deps=self.capture(validation=True)
        collected=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertFalse(collected['complete'])

    def test_real_scheduler_jitter_preserves_complete_mail_report_window(self):
        at=self.friday+timedelta(seconds=23,microseconds=500000)
        self.ws._collect_mail(self.settings,at)
        _,start,end=self.mail.call_args.args
        self.assertLessEqual(start,report_window(at)[0]);self.assertGreaterEqual(end,self.friday)
        _,deps=self.capture(validation=True)
        self.assertTrue(deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))['complete'])

    def test_hiworks_receipt_also_covers_cutoff_with_scheduler_jitter(self):
        settings={**self.settings,'hiworks_enabled':True,'hiworks_company':'company.invalid'}
        self.ws.bridge=Mock()
        self.ws.bridge.collect.return_value={'status':'completed','complete':True,'warnings':[],'documents':0}
        self.ws._collect_hiworks(settings,self.friday+timedelta(seconds=23,microseconds=500000))
        quality=self.ws._archive_quality('hiworks:company.invalid',*report_window(self.at))
        self.assertTrue(quality['complete'])

    def test_message_limit_is_a_real_failure_for_unpinned_fallback(self):
        self.store.connection.execute("DELETE FROM archive_scans WHERE scope LIKE 'outlook:%'")
        original=self.ws.messages
        self.ws.messages=Mock(side_effect=lambda *args,**kwargs:{**original(*args,**kwargs),'hasMore':True})
        _,deps=self.capture(validation=True,scope_acknowledged=True)
        collected=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertFalse(collected['complete']);self.assertIn('archive_message_limit_exceeded',collected['warnings'])

    def test_complete_receipt_is_not_limited_by_old_archive_history(self):
        scope='outlook:'+self.settings['mailbox']
        historical=[{**self.message,'source_id':f'historical-{index}',
                     'timestamp':'2025-01-01T10:00:00+09:00'} for index in range(10000)]
        self.store.ingest_messages(scope,historical)
        self.assertTrue(self.ws.messages(0,10000)['hasMore'])
        quality=self.ws._archive_quality(scope,*report_window(self.at))
        self.assertTrue(quality['complete']);self.assertEqual(1,len(quality['versionIds']))
        _,deps=self.capture(validation=True,scope_acknowledged=True)
        collected=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertTrue(collected['complete']);self.assertEqual([],collected['warnings'])
        self.assertEqual([self.message],collected['messages']);self.sender.assert_not_called()

    def test_oversized_pinned_receipt_remains_a_real_failure(self):
        scope='outlook:'+self.settings['mailbox']
        messages=[{**self.message,'source_id':f'current-{index}'} for index in range(10001)]
        self.store.ingest_messages(scope,messages)
        self.ws._record_collection(scope,{'messages':messages,'complete':True,'warnings':[]},
                                  report_window(self.at)[0],self.friday)
        quality=self.ws._archive_quality(scope,*report_window(self.at))
        self.assertEqual(10001,len(quality['versionIds']));self.assertFalse(quality['complete'])
        self.assertIn('archive_message_limit_exceeded',quality['warnings'])
        _,deps=self.capture(validation=True,scope_acknowledged=True)
        collected=deps['mail_collector'](self.settings['mailbox'],*report_window(self.at))
        self.assertFalse(collected['complete']);self.assertIn('archive_message_limit_exceeded',collected['warnings'])
        self.sender.assert_not_called()

    def test_enabled_hiworks_without_proven_receipt_is_blocking(self):
        settings={**self.settings,'hiworks_enabled':True,'hiworks_company':'company.invalid'}
        self.ws.save_settings(settings)
        _,deps=self.capture(settings,validation=True,scope_acknowledged=True)
        with patch('ariadne.source_text.extract_text',return_value='Synthetic meeting context'):
            _,warnings,_=deps['extra_sources'](*report_window(self.at))
        self.assertIn('hiworks_archive_collection_unverified',warnings)

    def test_failed_hiworks_status_cannot_claim_complete_collection(self):
        settings={**self.settings,'hiworks_enabled':True,'hiworks_company':'company.invalid'}
        self.ws.save_settings(settings)
        self.ws.bridge=Mock()
        self.ws.bridge.collect.return_value={'status':'failed','complete':True,'warnings':[],'documents':0}
        self.ws._collect_hiworks(settings,self.friday)
        _,deps=self.capture(settings,validation=True,scope_acknowledged=True)
        with patch('ariadne.source_text.extract_text',return_value='Synthetic meeting context'):
            _,warnings,_=deps['extra_sources'](*report_window(self.at))
        self.assertIn('hiworks_collection_incomplete',warnings)

    def test_verified_empty_hiworks_scan_is_a_disclosed_scope_limitation(self):
        settings={**self.settings,'hiworks_enabled':True,'hiworks_company':'company.invalid'}
        self.ws.save_settings(settings)
        self.ws.bridge=Mock()
        self.ws.bridge.collect.return_value={'status':'completed','complete':True,'warnings':[],'documents':0}
        self.ws._collect_hiworks(settings,self.friday)
        _,deps=self.capture(settings,validation=True,scope_acknowledged=True)
        with patch('ariadne.source_text.extract_text',return_value='Synthetic meeting context'):
            _,warnings,limitations=deps['extra_sources'](*report_window(self.at))
        self.assertNotIn('archived_hiworks_empty',warnings)
        self.assertIn('hiworks_current_period_evidence_empty',limitations)

    def test_teams_known_screen_bounds_do_not_hide_real_failure(self):
        settings={**self.settings,'teams_chats':['allowed']}
        self.ws.save_settings(settings)
        self.ws.teams=Mock(return_value={'messages':[],'complete':False,
                                       'warnings':['teams_screen_scope_only','teams_in_use_collection_deferred']})
        self.ws._collect_teams(settings,self.friday.replace(hour=12))
        _,deps=self.capture(settings,validation=True,scope_acknowledged=True)
        teams=deps['teams_collector'](['allowed'])
        self.assertFalse(teams['complete']);self.assertIn('teams_in_use_collection_deferred',teams['warnings'])

    def test_undated_teams_message_is_context_only_and_not_whole_week_complete(self):
        settings={**self.settings,'teams_chats':['allowed']}
        self.ws.save_settings(settings)
        self.ws.teams=Mock(return_value={'messages':[{'source_id':'allowed:message-body-1','provider':'teams_uia',
            'chat':'allowed','subject':'Synthetic','body':'Undated work','timestamp':None}],
            'complete':False,'warnings':['teams_screen_scope_only','teams_message_dates_unverified']})
        self.ws._collect_teams(settings,self.friday.replace(hour=12))
        self.store.connection.execute("UPDATE archive_versions SET observed_at=? WHERE scope LIKE 'teams:%'",
                                      (self.friday.replace(hour=12).isoformat(),))
        _,deps=self.capture(settings,validation=True)
        teams=deps['teams_collector'](['allowed'])
        self.assertTrue(teams['complete']);self.assertTrue(teams['messages'][0]['contextOnly'])
        self.assertIn('teams_message_dates_unverified',teams['limitations'])
        receipt=json.loads(self.store.connection.execute(
            "SELECT summary FROM archive_scans WHERE scope LIKE 'teams:%'").fetchone()['summary'])
        self.assertFalse(receipt['complete']);self.assertTrue(receipt['scopeComplete'])

    def test_old_teams_screen_snapshot_is_unverified(self):
        settings={**self.settings,'teams_chats':['allowed']}
        self.ws.save_settings(settings)
        self.ws.teams=Mock(return_value={'messages':[],'complete':False,'warnings':['teams_screen_scope_only']})
        self.ws._collect_teams(settings,self.friday-timedelta(days=2))
        _,deps=self.capture(settings,validation=True)
        self.assertFalse(deps['teams_collector'](['allowed'])['complete'])

    def test_teams_analysis_flags_never_enter_raw_source_archive(self):
        settings={**self.settings,'teams_chats':['allowed']}
        self.ws.save_settings(settings)
        message={'source_id':'allowed:message-body-1','provider':'teams_uia','chat':'allowed',
                 'subject':'Synthetic','body':'Undated work','timestamp':None}
        self.ws.teams=Mock(return_value={'messages':[message],'complete':False,
                                      'warnings':['teams_screen_scope_only','teams_message_dates_unverified']})
        self.ws._collect_teams(settings,self.friday.replace(hour=12))
        self.store.connection.execute("UPDATE archive_versions SET observed_at=? WHERE scope LIKE 'teams:%'",
                                      (self.friday.replace(hour=12).isoformat(),))
        scope='teams:'+settings['mailbox'];version=self.store.latest(scope)[message['source_id']]['id']
        report={'items':[{'project':'Synthetic','work':'Review','result':'Reviewed','issues':'',
                         'next_week':'Follow up','evidence_ids':['S1']}]}
        self.ws.reporter=Mock(return_value=(report,[{**self.message,'evidence_id':'S1'}]))
        def write(output,*args):
            output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'synthetic-report')
        with patch('ariadne.weekly_worker.data_directory',return_value=self.base), \
                patch('ariadne.weekly_worker.write_report',side_effect=write), \
                patch('ariadne.meeting_report.write_meeting_report',side_effect=write), \
                patch('ariadne.source_text.extract_text',return_value='Synthetic meeting context'), \
                patch('ariadne.hiworks.DraftStore') as drafts:
            drafts.return_value.save.return_value={'id':'synthetic-draft'}
            result=self.ws.prepare_weekly(settings,self.at,validation=True,scope_acknowledged=True)
        self.assertEqual('prepared',result['status'])
        self.assertEqual(version,self.store.latest(scope)[message['source_id']]['id'])
        self.assertNotIn('contextOnly',json.loads(self.store.read_version(version)))

    def test_validation_wrapper_shares_durable_operation_lock(self):
        self.store.connection.execute("INSERT INTO workspace_jobs VALUES('busy','mail','running',?,NULL,'{}')",(now(),))
        with self.assertRaises(ValidationError):self.ws.prepare_validation(True)
        self.sender.assert_not_called()

    def test_validation_wrapper_records_sanitized_failure(self):
        with patch.object(self.ws,'prepare_weekly',side_effect=RuntimeError('secret body must not enter job logs')):
            with self.assertRaises(RuntimeError):self.ws.prepare_validation(True)
        job=self.ws.jobs()[0]
        self.assertEqual('failed',job['status']);self.assertNotIn('secret',json.dumps(job))

    def test_send_and_confirm_require_validation_owner(self):
        suffix=hashlib.sha256(self.settings['mailbox'].encode()).hexdigest()[:12]
        identifier=f'2026-10-02-{suffix}-validation-{uuid4()}'
        with patch('ariadne.weekly_worker.send_prepared',return_value={'status':'submitted'}) as send:
            self.ws.send_validation(identifier,'a'*64)
            self.assertEqual(self.sender,send.call_args.kwargs['sender'])
            with self.assertRaises(ValidationError):self.ws.send_validation(f'2026-10-02-{suffix}','a'*64)
            with self.assertRaises(ValidationError):self.ws.send_validation(f'2026-10-02-000000000000-validation-{uuid4()}','a'*64)
            self.assertEqual(1,send.call_count)
        with patch('ariadne.weekly_worker.confirm_delivery_run',return_value={'status':'delivery_pending'}) as confirm:
            self.ws.confirm_validation(identifier)
            self.assertEqual(identifier,confirm.call_args.args[2])

    def test_non_omen_validation_wrapper_refuses_before_helper_call(self):
        self.ws.save_settings({**self.settings,'device_role':'single'})
        with patch('ariadne.weekly_worker.send_prepared') as send:
            with self.assertRaises(ValidationError):self.ws.send_validation('synthetic','a'*64)
            send.assert_not_called()


if __name__=='__main__':
    unittest.main()
