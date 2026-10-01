import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ariadne.automation_store import AutomationStore
from ariadne.cloud_handoff import (CloudError,MAGIC,SharePointClient,
    connection_status,deliver,profile_lock,upload_packets,validate_url)
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
    def get(self,url,**kwargs):
        if kwargs.get('max_redirects')!=0:raise AssertionError('Must not forward requests to login redirects')
        if self.redirect:return Response(302)
        if self.denied:return Response(403)
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


class CloudDeliveryTests(unittest.TestCase):
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

    def test_destination_is_exact_company_folder_and_role_is_company_only(self):
        self.assertEqual(WEB_URL,validate_url(WEB_URL))
        for url in ['http://example-my.sharepoint.com/my?id='+FOLDER,
                    WEB_URL.replace('example-my.sharepoint.com','evil.example'),
                    WEB_URL+'&id='+FOLDER,WEB_URL.replace('ARIADNE','other'),
                    WEB_URL.replace('https://','https://user@')]:
            with self.assertRaises(ValueError):validate_url(url)
        values={'device_role':'company-relay','handoff_transport':'web','handoff_web_url':WEB_URL}
        self.assertEqual('web',validate_settings(values)['handoff_transport'])
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
