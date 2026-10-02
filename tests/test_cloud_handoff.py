import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ariadne.automation_store import AutomationStore
from ariadne.cloud_handoff import (CloudError,MAGIC,SharePointClient,
    connection_status,deliver,download_packets,receive,profile_lock,upload_packets,validate_url)
from ariadne.handoff import export_folder
from ariadne.local_settings import validate_settings
FOLDER='/personal/owner_example_com/Documents/ARIADNE'
WEB_URL='https://example-my.sharepoint.com/my?id=%2Fpersonal%2Fowner_example_com%2FDocuments%2FARIADNE'


class Response:
    def __init__(self,status=200,value=None,body=b''):
        self.status,self.value,self.payload=status,value,body
    def json(self):return self.value
    def body(self):return self.payload


class Remote:
    """Independent SharePoint contract fake, including ambiguous HTTP outcomes."""
    def __init__(self):
        self.files={};self.posts=[];self.redirect=False;self.denied=False;self.timeout_after_write=False
        self.list_pages=None;self.gets=[]
    def get(self,url,**kwargs):
        self.gets.append(url)
        if kwargs.get('max_redirects')!=0:raise AssertionError('Must not forward requests to login redirects')
        if self.redirect:return Response(302)
        if self.denied:return Response(403)
        if '/Files?' in url:
            if self.list_pages is not None:return Response(value={'d':self.list_pages.pop(0)})
            return Response(value={'d':{'results':[{'Name':name,'Length':str(len(data))} for name,data in self.files.items()]}})
        if 'GetFolderByServerRelativeUrl' in url:return Response(value={'d':{'ServerRelativeUrl':FOLDER}})
        name=url.split('/ARIADNE/')[1].split("'")[0]
        if name not in self.files:return Response(404)
        data=self.files[name]
        return Response(body=data) if url.endswith('/$value') else Response(value={'d':{'Length':str(len(data))}})
    def post(self,url,**kwargs):
        if kwargs.get('max_redirects')!=0:raise AssertionError('No cross-origin credential/digest forwarding')
        if url.endswith('/contextinfo'):
            return Response(value={'d':{'GetContextWebInformation':{'FormDigestValue':'synthetic-test-digest'}}})
        if 'overwrite=false' not in url:raise AssertionError('Immutable files must not be overwritten')
        name=url.split("add(url='")[1].split("'")[0]
        if name in self.files:return Response(409)
        self.files[name]=kwargs['data'];self.posts.append(name)
        if self.timeout_after_write:self.timeout_after_write=False;raise TimeoutError('synthetic secret must not appear in errors')
        return Response(201)


class CloudFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'shared';self.source.mkdir();(self.source/'meeting.txt').write_text('meeting')
        self.spool=self.root/'outgoing-packets';self.spool.mkdir()
        self.store=AutomationStore(self.root/'automation.sqlite3');self.key=b'x'*32
        self.remote=Remote();self.client=SharePointClient(Mock(request=self.remote),WEB_URL)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def publish(self):
        self.store.sync_folder(self.source)
        export_folder(self.store,self.source,self.spool,self.key)
        return sorted(self.spool.glob('*.ariadne-packet'))[-1]


class CloudDeliveryTests(CloudFixture,unittest.TestCase):
    def test_ordered_ciphertext_delivery_is_verified_and_idempotent(self):
        first=self.publish();(self.source/'meeting.txt').write_text('next');second=self.publish()
        result=upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertEqual({'status':'uploaded','packets':2,'verified':True},result)
        self.assertEqual([first.name,second.name],self.remote.posts)
        for path in (first,second):self.assertEqual(path.read_bytes(),self.remote.files[path.name])
        self.assertEqual(0,upload_packets(self.store,self.root,WEB_URL,self.client)['packets'])
        self.assertEqual(2,len(self.remote.posts))
        self.assertEqual('connected',connection_status(self.root)['status'])

    def test_manual_bootstrap_file_is_checked_without_overwrite(self):
        path=self.publish();self.remote.files[path.name]=path.read_bytes()
        self.assertEqual(1,upload_packets(self.store,self.root,WEB_URL,self.client)['packets'])
        self.assertEqual([],self.remote.posts)

    def test_ambiguous_upload_result_recovers_without_duplicate_write(self):
        self.publish();self.remote.timeout_after_write=True
        with self.assertRaises(CloudError):upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertEqual(0,self.store.connection.execute('SELECT COUNT(*) FROM handoff_cloud_uploads').fetchone()[0])
        upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertEqual(1,len(self.remote.posts))

    def test_conflicting_remote_bytes_are_never_overwritten(self):
        path=self.publish();self.remote.files[path.name]=b'z'*path.stat().st_size
        with self.assertRaises(CloudError) as error:upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertEqual('remote_conflict',error.exception.code);self.assertEqual([],self.remote.posts)

    def test_missing_predecessor_prevents_later_upload(self):
        first=self.publish();self.publish();first.unlink()
        with self.assertRaises(CloudError) as error:upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertEqual('packet_missing',error.exception.code);self.assertEqual([],self.remote.posts)

    def test_plaintext_or_unknown_file_is_not_uploaded(self):
        path=self.publish();path.write_bytes(b'private plaintext'*20)
        with self.assertRaises(CloudError):upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertEqual([],self.remote.posts)
        with self.assertRaises(CloudError):self.client.put_verified('password.txt',MAGIC+b'x'*100)

    def test_changed_local_ciphertext_after_confirmation_is_rejected(self):
        path=self.publish();upload_packets(self.store,self.root,WEB_URL,self.client)
        raw=path.read_bytes();path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with self.assertRaises(CloudError):upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertEqual(1,len(self.remote.posts))

    def test_authentication_redirect_or_denial_stops_before_upload(self):
        self.publish()
        for attribute,code in [('redirect','login_required'),('denied','permission_denied')]:
            setattr(self.remote,attribute,True)
            with self.assertRaises(CloudError) as error:upload_packets(self.store,self.root,WEB_URL,self.client)
            self.assertEqual(code,error.exception.code);setattr(self.remote,attribute,False)
        self.assertEqual([],self.remote.posts)

    def test_failure_returns_safe_actionable_state_and_keeps_packet(self):
        path=self.publish()
        with patch('ariadne.cloud_handoff.browser_client',side_effect=CloudError('login_required')):
            result=deliver(self.store,self.root,WEB_URL)
        self.assertEqual('failed',result['status']);self.assertTrue(path.exists())
        self.assertEqual('login_required',connection_status(self.root)['code'])

    def test_unexpected_exception_does_not_disclose_transport_secrets(self):
        with patch('ariadne.cloud_handoff.browser_client',side_effect=RuntimeError('synthetic-secret-cookie')):
            result=deliver(self.store,self.root,WEB_URL)
        self.assertNotIn('synthetic-secret-cookie',str(result));self.assertEqual('failed',result['status'])

    def test_profile_lock_prevents_interference_and_is_released(self):
        with profile_lock(self.root):
            with self.assertRaises(CloudError) as error:
                with profile_lock(self.root):pass
            self.assertEqual('connection_busy',error.exception.code)
        with profile_lock(self.root):pass

    def test_destination_is_exact_company_folder_and_hybrid_roles_only(self):
        self.assertEqual(WEB_URL,validate_url(WEB_URL))
        for url in ['http://example-my.sharepoint.com/my?id='+FOLDER,
                    WEB_URL.replace('example-my.sharepoint.com','evil.example'),
                    WEB_URL+'&id='+FOLDER,WEB_URL.replace('ARIADNE','other'),
                    WEB_URL.replace('https://','https://user@')]:
            with self.assertRaises(ValueError):validate_url(url)
        values={'device_role':'company-relay','handoff_transport':'web','handoff_web_url':WEB_URL}
        self.assertEqual('web',validate_settings(values)['handoff_transport'])
        self.assertEqual('web',validate_settings({**values,'device_role':'omen','handoff_required':True})['handoff_transport'])
        with self.assertRaises(ValueError):validate_settings({**values,'device_role':'single'})
        with self.assertRaises(ValueError):validate_settings({**values,'handoff_web_url':''})

    def test_server_folder_identity_mismatch_is_rejected(self):
        request=Mock()
        request.get.return_value=Response(value={'d':{'ServerRelativeUrl':FOLDER+'/different'}})
        with self.assertRaises(CloudError) as error:SharePointClient(Mock(request=request),WEB_URL).check_folder()
        self.assertEqual('permission_denied',error.exception.code);request.post.assert_not_called()

    def test_size_limit_stops_before_network_write(self):
        path=self.publish()
        with patch('ariadne.cloud_handoff.MAX_PACKET',45):
            with self.assertRaises(CloudError):upload_packets(self.store,self.root,WEB_URL,self.client)
        self.assertTrue(path.exists());self.assertEqual([],self.remote.posts)

    @unittest.skipUnless(os.name=='nt','Windows connection launcher')
    def test_active_connection_helper_is_not_started_twice(self):
        from ariadne.cloud_handoff import open_connection
        (self.root/'cloud-connect.pid').write_text('123')
        with patch('ariadne.workspace.process_alive',return_value=True),patch('ariadne.cloud_handoff.subprocess.Popen') as launch:
            with self.assertRaises(CloudError) as error:open_connection(self.root,WEB_URL)
            self.assertEqual('connection_busy',error.exception.code);launch.assert_not_called()

    def test_workspace_preserves_local_archive_when_cloud_fails(self):
        from ariadne.repository import Repository
        from ariadne.workspace import Workspace
        self.store.sync_folder(self.source)
        repo=Repository(self.root/'domain.sqlite3');ws=Workspace(self.store,self.root/'automation.json',repo)
        credentials=self.root/'credentials';credentials.mkdir();(credentials/'shared-folder.dpapi').write_bytes(b'synthetic')
        settings=validate_settings({'device_role':'company-relay','folder':str(self.source),
            'handoff_transport':'web','handoff_web_url':WEB_URL,'handoff_dir':str(self.root/'unused-personal')})
        try:
            with patch('ariadne.handoff.read_key',return_value=self.key),patch('ariadne.cloud_handoff.deliver',return_value={'status':'failed','message':'login required'}) as send:
                result=ws._export_handoff(settings)
            self.assertEqual('failed',result['status']);self.assertEqual(1,len(list(self.spool.glob('*.ariadne-packet'))))
            self.assertFalse((self.root/'unused-personal').exists());self.assertEqual(self.root,send.call_args.args[1])
            self.assertEqual(1,len(self.store.latest('folder:'+str(self.source.resolve()).casefold())))
        finally:ws.close();repo.close()


class CloudReceiveTests(CloudFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.destination=self.root/'omen';self.destination.mkdir()
        self.cache=self.destination/'shared-meetings'
        self.receiver=AutomationStore(self.destination/'automation.sqlite3')
    def tearDown(self):self.receiver.close();super().tearDown()
    def transfer(self):
        path=self.publish();self.remote.files[path.name]=path.read_bytes();return path
    def download(self,key=None,limit=20):
        return download_packets(self.receiver,self.destination,WEB_URL,self.cache,key or self.key,self.client,limit=limit)

    def test_receiver_imports_version_history_and_does_not_write_cloud(self):
        self.transfer();(self.source/'meeting.txt').write_text('next');self.transfer()
        result=self.download()
        self.assertEqual(2,result['packets']);self.assertEqual('next',(self.cache/'meeting.txt').read_text())
        self.assertEqual(2,self.receiver.connection.execute('SELECT COUNT(*) FROM archive_versions').fetchone()[0])
        self.assertEqual([],self.remote.posts);self.assertEqual('connected',connection_status(self.destination)['status'])
        self.assertEqual(0,self.download()['packets'])

    def test_gap_blocks_import_before_touching_db(self):
        first=self.transfer();self.transfer();del self.remote.files[first.name]
        with self.assertRaises(CloudError) as error:self.download()
        self.assertEqual('packet_missing',error.exception.code)
        self.assertEqual(0,self.receiver.connection.execute('SELECT COUNT(*) FROM handoff_imports').fetchone()[0])

    def test_wrong_key_or_tampering_does_not_import(self):
        path=self.transfer()
        with self.assertRaises(CloudError) as error:self.download(b'y'*32)
        self.assertEqual('packet_authentication',error.exception.code)
        self.assertEqual(0,self.receiver.connection.execute('SELECT COUNT(*) FROM handoff_imports').fetchone()[0])
        raw=self.remote.files[path.name];self.remote.files[path.name]=raw[:-1]+bytes([raw[-1]^1])
        # Use a fresh spool to exercise authentication rather than saved-byte conflict.
        (self.destination/'incoming-packets'/path.name).unlink()
        with self.assertRaises(CloudError) as error:self.download()
        self.assertEqual('packet_authentication',error.exception.code)
        self.assertFalse((self.cache/'meeting.txt').exists())

    def test_filename_must_match_authenticated_stream(self):
        path=self.transfer();raw=self.remote.files.pop(path.name)
        foreign='11111111-1111-4111-8111-111111111111'+path.name[36:]
        self.remote.files[foreign]=raw
        with self.assertRaises(CloudError) as error:self.download()
        self.assertEqual('packet_authentication',error.exception.code)
        self.assertEqual(0,self.receiver.connection.execute('SELECT COUNT(*) FROM archive_versions').fetchone()[0])

    def test_other_sender_never_silently_replaces_prior(self):
        path=self.transfer();self.download()
        foreign='11111111-1111-4111-8111-111111111111'+path.name[36:]
        self.remote.files={foreign:path.read_bytes()}
        with self.assertRaises(CloudError) as error:self.download()
        self.assertEqual('sender_conflict',error.exception.code)

    def test_two_senders_require_explicit_recovery(self):
        path=self.transfer();self.remote.files['11111111-1111-4111-8111-111111111111'+path.name[36:]]=path.read_bytes()
        with self.assertRaises(CloudError) as error:self.download()
        self.assertEqual('sender_conflict',error.exception.code)

    def test_bounded_backlog_continues_next_run(self):
        for _ in range(3):self.transfer()
        first=self.download(limit=2);second=self.download(limit=2)
        self.assertEqual((2,1),(first['packets'],first['pending']))
        self.assertEqual((1,0),(second['packets'],second['pending']))
        self.assertEqual(3,second['lastReceived']['sequence'])

    def test_unrelated_cloud_files_are_not_downloaded(self):
        self.transfer();self.remote.files['application.zip']=b'installer';self.remote.files['notes.txt']=b'notes'
        self.assertEqual(1,self.download()['packets'])
        self.assertFalse(any('application.zip' in url or 'notes.txt' in url for url in self.remote.gets))

    def test_pagination_stays_on_exact_file_collection(self):
        path=self.transfer();base=self.client.site+"/_api/web/GetFolderByServerRelativeUrl('"+FOLDER+"')/Files"
        row={'Name':path.name,'Length':str(path.stat().st_size)}
        self.remote.list_pages=[{'results':[],'__next':base+'?$select=Name,Length&$top=100&$skiptoken=Paged%3DTRUE'}, {'results':[row]}]
        self.assertEqual(1,self.download()['packets'])
        self.assertEqual([],self.remote.posts)

    def test_pagination_cannot_read_other_origin_or_folder(self):
        for next_url in ['https://evil.example/_api/files',self.client.site+"/_api/web/GetFolderByServerRelativeUrl('/other')/Files?$select=Name,Length"]:
            self.remote.list_pages=[{'results':[],'__next':next_url}]
            with self.assertRaises(CloudError):self.download()
            self.assertNotIn(next_url,self.remote.gets)

    def test_invalid_packet_metadata_stops_before_download(self):
        path=self.transfer()
        for row in [{'Name':'bad.ariadne-packet','Length':'100'},{'Name':path.name,'Length':str(65*1024*1024)}]:
            self.remote.list_pages=[{'results':[row]}]
            with self.assertRaises(CloudError):self.download()
        self.assertFalse(any(url.endswith('/$value') for url in self.remote.gets))

    def test_download_size_change_or_missing_file_is_rejected(self):
        path=self.transfer()
        with self.assertRaises(CloudError):self.client.download_packet(path.name,path.stat().st_size+1)
        del self.remote.files[path.name]
        with self.assertRaises(CloudError) as error:self.client.download_packet(path.name,path.stat().st_size)
        self.assertEqual('packet_missing',error.exception.code)

    def test_receive_failure_is_safe_and_visible(self):
        with patch('ariadne.cloud_handoff.browser_client',side_effect=CloudError('login_required')):
            result=receive(self.receiver,self.destination,WEB_URL,self.cache,self.key)
        self.assertEqual('failed',result['status']);self.assertEqual('login_required',connection_status(self.destination)['code'])

    def test_web_receipt_preserves_friday_inputs_after_later_edits(self):
        from datetime import datetime
        from ariadne.reporting import KST
        from ariadne.weekly_archive import archive_weekly,archive_files,archive_runs
        archive_weekly(self.store,{'folder':str(self.source),'weekly_archive_enabled':True},datetime(2026,10,2,16,tzinfo=KST))
        self.transfer();self.download()
        week=archive_runs(self.receiver)[0];original=archive_files(self.receiver,week['id'])
        (self.source/'meeting.txt').write_text('after four');self.transfer();self.download()
        self.assertEqual(original,archive_files(self.receiver,week['id']))
        self.assertEqual(b'meeting',self.receiver.read_version(original[0]['id']))
        self.assertEqual('after four',(self.cache/'meeting.txt').read_text())

    def test_web_receipt_preserves_deleted_file_history(self):
        from ariadne.automation_store import canonical
        self.transfer();self.download()
        (self.source/'meeting.txt').unlink();self.transfer();self.download()
        history=self.receiver.history('folder:'+canonical(self.cache))
        self.assertEqual(2,len(history));self.assertFalse((self.cache/'meeting.txt').exists())
        self.assertEqual(b'meeting',self.receiver.read_version(next(row['id'] for row in history if row['version']==1)))

    def test_workspace_uses_web_without_local_onedrive_and_blocks_failed_receive(self):
        from ariadne.models import ValidationError
        from ariadne.repository import Repository
        from ariadne.workspace import Workspace
        from ariadne.desktop import initial_settings
        repo=Repository(self.destination/'domain.sqlite3');ws=Workspace(self.receiver,self.destination/'automation.json',repo)
        settings=initial_settings('omen',self.destination,{'company_web_url':WEB_URL})
        self.assertEqual('',settings['handoff_dir']);self.assertEqual('web',settings['handoff_transport'])
        try:
            with patch('ariadne.handoff.read_key',return_value=self.key),patch('ariadne.cloud_handoff.receive',return_value={'status':'failed','message':'connection failed'}) as read:
                with self.assertRaises(ValidationError):ws._import_handoff(settings)
                self.assertEqual(self.destination,read.call_args.args[1])
            with patch.object(ws,'_import_handoff',side_effect=ValidationError('connection failed')),patch.object(ws,'reporter') as ai:
                with self.assertRaises(ValidationError):ws.prepare_weekly(settings)
                ai.assert_not_called()
        finally:ws.close();repo.close()
