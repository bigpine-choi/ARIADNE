"""Two independent archives, authenticated delivery, and immutable Friday inputs."""
from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZIP_DEFLATED

from ariadne.automation_store import AutomationStore, canonical
from ariadne import handoff
from ariadne.desktop import initial_settings, schedule_xml, seed_settings
from ariadne.local_settings import validate_settings
from ariadne.models import ValidationError
from ariadne.repository import Repository
from ariadne.reporting import KST
from ariadne.weekly_archive import archive_weekly, archive_files, archive_runs
from ariadne.workspace import Workspace


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.source=self.base/'company';self.source.mkdir();(self.source/'one.txt').write_bytes(b'original')
        self.cloud=self.base/'OneDrive';self.cloud.mkdir();self.cache=self.base/'omen/shared-meetings'
        self.company=AutomationStore(self.base/'company.sqlite3');self.omen=AutomationStore(self.base/'omen.sqlite3')
        self.key=bytes(range(32))
        self.friday=datetime(2026,10,2,16,tzinfo=KST)

    def tearDown(self):
        self.company.close();self.omen.close();self.tmp.cleanup()

    def publish(self):
        self.company.sync_folder(self.source)
        result=handoff.export_folder(self.company,self.source,self.cloud,self.key)
        return sorted(self.cloud.glob('*.ariadne-packet'))[-1],result

    def rewrite(self,path,change):
        with ZipFile(BytesIO(handoff.unseal(path.read_bytes(),self.key))) as z:
            entries={name:z.read(name) for name in z.namelist()}
        manifest=json.loads(entries['manifest.json']);change(manifest)
        entries['manifest.json']=json.dumps(manifest).encode();raw=BytesIO()
        with ZipFile(raw,'w',ZIP_DEFLATED) as z:
            for name,data in entries.items():z.writestr(name,data)
        path.write_bytes(handoff.seal(raw.getvalue(),self.key))

    def test_folder_only_delivery_preserves_bytes_and_no_other_sources(self):
        self.company.ingest_messages('outlook:private@example.invalid',[{'source_id':'private','body':'never delivered'}])
        path,result=self.publish();self.assertEqual(1,result['versions'])
        self.assertNotIn(b'original',path.read_bytes());self.assertNotIn(b'never delivered',path.read_bytes())
        self.assertEqual('imported',handoff.import_packet(self.omen,path,self.cache,self.key)['status'])
        self.assertEqual(b'original',(self.cache/'one.txt').read_bytes())
        self.assertEqual([],self.omen.history('outlook:private@example.invalid'))
        self.assertEqual(b'original',(self.source/'one.txt').read_bytes())

    def test_wrong_key_and_tamper_never_change_target_archive(self):
        path,_=self.publish()
        for payload,key in [(path.read_bytes(),b'x'*32),(path.read_bytes()[:-1]+bytes([path.read_bytes()[-1]^1]),self.key)]:
            bad=self.cloud/'bad.ariadne-packet';bad.write_bytes(payload)
            with self.assertRaises(Exception):handoff.import_packet(self.omen,bad,self.cache,key)
        self.assertEqual([],self.omen.history('folder:'+canonical(self.cache)))
        self.assertFalse(self.cache.exists())

    def test_changed_and_deleted_versions_and_replay_are_idempotent(self):
        first,_=self.publish();handoff.import_packet(self.omen,first,self.cache,self.key)
        (self.source/'one.txt').write_bytes(b'changed');second,_=self.publish()
        handoff.import_packet(self.omen,second,self.cache,self.key)
        (self.source/'one.txt').unlink();third,_=self.publish()
        handoff.import_packet(self.omen,third,self.cache,self.key)
        rows=self.omen.history('folder:'+canonical(self.cache))
        self.assertEqual(3,len(rows));self.assertFalse((self.cache/'one.txt').exists())
        self.assertEqual('already_imported',handoff.import_packet(self.omen,first,self.cache,self.key)['status'])
        self.assertEqual(0,handoff.import_available(self.omen,self.cloud,self.cache,self.key)['packets'])
        self.assertEqual(b'original',self.omen.read_version(next(r['id'] for r in rows if r['version']==1)))

    def test_missing_predecessor_blocks_import_then_recovers(self):
        first,_=self.publish();(self.source/'one.txt').write_bytes(b'next');second,_=self.publish()
        with self.assertRaisesRegex(ValueError,'missing packet'):handoff.import_packet(self.omen,second,self.cache,self.key)
        self.assertFalse(self.cache.exists())
        handoff.import_packet(self.omen,first,self.cache,self.key)
        handoff.import_packet(self.omen,second,self.cache,self.key)
        self.assertEqual(b'next',(self.cache/'one.txt').read_bytes())

    def test_failed_publication_recovers_exact_packet_before_next_sequence(self):
        self.company.sync_folder(self.source)
        with patch('ariadne.handoff._write_packet',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):handoff.export_folder(self.company,self.source,self.cloud,self.key)
        row=self.company.connection.execute('SELECT * FROM handoff_outbox').fetchone();sealed=bytes(row['payload'])
        _,result=self.publish();self.assertEqual(2,result['sequence'])
        paths=sorted(self.cloud.glob('*.ariadne-packet'));self.assertEqual(sealed,paths[0].read_bytes())
        self.assertEqual(2,handoff.import_available(self.omen,self.cloud,self.cache,self.key)['packets'])

    def test_weekly_manifest_keeps_original_version_after_later_edits(self):
        archive_weekly(self.company,{'folder':str(self.source),'weekly_archive_enabled':True},self.friday)
        first,_=self.publish();handoff.import_packet(self.omen,first,self.cache,self.key)
        week=next(r for r in archive_runs(self.omen) if r['week_end']==self.friday.isoformat())
        original=archive_files(self.omen,week['id'])
        (self.source/'one.txt').write_bytes(b'after four');second,_=self.publish();handoff.import_packet(self.omen,second,self.cache,self.key)
        self.assertEqual(original,archive_files(self.omen,week['id']))
        self.assertEqual(b'original',self.omen.read_version(original[0]['id']))

    def test_path_traversal_extra_metadata_and_inconsistent_size_rejected(self):
        changes=[lambda m:m['versions'][0].update(source_id='../escape.txt'),
            lambda m:m['versions'][0]['metadata'].update(apiKey='synthetic'),
            lambda m:m['versions'][0]['metadata'].update(size=999)]
        path,_=self.publish();original=path.read_bytes()
        for change in changes:
            path.write_bytes(original);self.rewrite(path,change)
            with self.assertRaises(ValueError):handoff.import_packet(self.omen,path,self.cache,self.key)
        self.assertEqual([],self.omen.history('folder:'+canonical(self.cache)))

    def test_same_sequence_with_different_content_rejected(self):
        path,_=self.publish();handoff.import_packet(self.omen,path,self.cache,self.key)
        self.rewrite(path,lambda m:m.update(packet_id='00000000-0000-4000-8000-000000000001'))
        with self.assertRaisesRegex(ValueError,'conflict'):handoff.import_packet(self.omen,path,self.cache,self.key)

    def test_sync_destination_inside_source_rejected(self):
        self.company.sync_folder(self.source)
        with self.assertRaises(ValueError):handoff.export_folder(self.company,self.source,self.source,self.key)

    def test_omen_report_pins_template_to_friday_manifest(self):
        (self.source/'template.xlsx').write_bytes(b'friday template')
        archive_weekly(self.company,{'folder':str(self.source),'weekly_archive_enabled':True},self.friday)
        first,_=self.publish();handoff.import_packet(self.omen,first,self.cache,self.key)
        (self.source/'template.xlsx').write_bytes(b'later template')
        second,_=self.publish();handoff.import_packet(self.omen,second,self.cache,self.key)
        repo=Repository(self.base/'domain.sqlite3')
        ws=Workspace(self.omen,self.cache.parent/'automation.json',repo)
        settings=initial_settings('omen',self.cache.parent,{'mailbox':'owner@example.invalid','template_relative':'template.xlsx'})
        try:
            ws.save_settings(settings)
            with patch.object(ws,'_import_handoff',return_value={'status':'imported'}),patch('ariadne.workspace.run_weekly',return_value={'status':'prepared'}) as runner:
                ws.prepare_weekly(settings,self.friday+timedelta(hours=2))
                self.assertEqual(b'friday template',runner.call_args.kwargs['template_bytes'])
            self.assertEqual(b'later template',(self.cache/'template.xlsx').read_bytes())
        finally:ws.close();repo.close()


class HybridOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.store=AutomationStore(self.base/'archive.sqlite3');self.repo=Repository(self.base/'domain.sqlite3')
        self.mail=Mock();self.teams=Mock();self.sender=Mock()
        self.ws=Workspace(self.store,self.base/'automation.json',self.repo,mail=self.mail,teams=self.teams,sender=self.sender)

    def tearDown(self):self.ws.close();self.store.close();self.repo.close();self.tmp.cleanup()

    def test_company_relay_cannot_enable_mail_teams_ai_or_send(self):
        settings=initial_settings('company-relay',self.base,{'company_folder':str(self.base/'shared')})
        self.ws.save_settings(settings)
        for name,value in [('mailbox','private@example.invalid'),('teams_chats',['private']),('enabled',True),('auto_send',True),('hiworks_enabled',True)]:
            with self.assertRaises(ValueError):validate_settings({**settings,name:value})
        for action in ['mail','teams','hiworks','weekly']:
            with self.assertRaises(ValidationError):self.ws.start(action)
        self.ws.collect(settings);self.mail.assert_not_called();self.teams.assert_not_called();self.sender.assert_not_called()

    def test_missing_current_friday_manifest_blocks_ai_and_email(self):
        settings=initial_settings('omen',self.base,{'mailbox':'owner@example.invalid'})
        self.ws.save_settings(settings)
        friday=datetime(2026,10,2,16,15,tzinfo=KST)
        with patch.object(self.ws,'_import_handoff',return_value={'status':'waiting'}),patch('ariadne.workspace.run_weekly') as runner:
            with self.assertRaisesRegex(ValidationError,'이번 금요일'):self.ws.prepare_weekly(settings,friday)
            runner.assert_not_called()
        self.sender.assert_not_called()

    def test_omen_may_not_recreate_company_manifest(self):
        settings=initial_settings('omen',self.base,{})
        self.ws.save_settings(settings)
        with self.assertRaises(ValueError):validate_settings({**settings,'weekly_archive_enabled':True})
        with self.assertRaises(ValueError):validate_settings({**settings,'handoff_required':False})
        with self.assertRaises(ValidationError):self.ws.start('archive')

    def test_first_run_seed_does_not_overwrite_existing_settings(self):
        first=seed_settings(self.base,'omen',{'mailbox':'first@example.invalid'})
        second=seed_settings(self.base,'omen',{'mailbox':'second@example.invalid'})
        self.assertEqual(first,second)
        with self.assertRaises(ValueError):seed_settings(self.base,'company-relay',{})

    def test_task_roles_use_owner_only_triggers_and_allow_laptop_battery(self):
        import xml.etree.ElementTree as ET
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        for role,count in [('company-relay',10),('omen',11)]:
            task=ET.fromstring(schedule_xml(role,'S-1-5-test','C:/runtime/pythonw.exe',self.base,self.base))
            self.assertEqual(count,len(task.findall('.//t:CalendarTrigger',ns)))
            self.assertEqual('InteractiveToken',task.find('.//t:LogonType',ns).text)
            self.assertEqual('LeastPrivilege',task.find('.//t:RunLevel',ns).text)
            self.assertEqual('Queue',task.find('.//t:MultipleInstancesPolicy',ns).text)
            self.assertEqual('false',task.find('.//t:DisallowStartIfOnBatteries',ns).text)
            self.assertEqual('false',task.find('.//t:StartWhenAvailable',ns).text)
            self.assertIn('--data-dir',task.find('.//t:Arguments',ns).text)

    def test_windows_task_xml_with_utf16_declaration_and_ascii_pipe_is_read(self):
        from ariadne.desktop import schedule_status
        xml=('<?xml version="1.0" encoding="UTF-16"?>'+schedule_xml('company-relay','S-1-test','pythonw.exe',self.base,self.base)).encode('utf-8')
        with patch('ariadne.desktop.os.name','nt'),patch('ariadne.desktop.subprocess.run',return_value=Mock(returncode=0,stdout=xml)):
            status=schedule_status('company-relay')
        self.assertEqual({'registered':True,'enabled':True,'triggers':10},status)
        xml=xml.replace(b'<Enabled>true</Enabled>',b'')
        with patch('ariadne.desktop.os.name','nt'),patch('ariadne.desktop.subprocess.run',return_value=Mock(returncode=0,stdout=xml)):
            self.assertTrue(schedule_status('company-relay')['enabled'])
