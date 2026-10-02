"""Read-only collector preflight API: no identifiers, source reads or writes."""
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from ariadne import local_mail, teams_uia
from ariadne.server import create_server


PATH = '/api/workspace/native-diagnostics'
STATUS = {
    'ready': True, 'host': 'windows_powershell_5_1',
    'persistentPolicyChanged': False, 'accountVerified': False,
    'version': '5.1.26100.1234', 'effectivePolicy': 'Restricted',
    'scopes': {scope: 'Undefined' for scope in
               ('MachinePolicy', 'UserPolicy', 'Process', 'CurrentUser', 'LocalMachine')},
    'groupPolicy': 'Undefined', 'processPolicyRequested': 'RemoteSigned',
    'codes': [],
}


class NativeDiagnosticsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = Mock()
        cls.workspace = Mock()
        cls.credentials = Mock()
        cls.server = create_server(cls.repository, port=0, workspace=cls.workspace,
                                   credential_store=cls.credentials)
        cls.port = cls.server.server_address[1]
        cls.origin = f'http://127.0.0.1:{cls.port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        for mock in (self.repository, self.workspace, self.credentials):
            mock.reset_mock(return_value=True, side_effect=True)

    def request(self, *, path=PATH, method='GET', headers=None):
        supplied = {'Origin': self.origin}
        supplied.update(headers or {})
        body = None
        if method in {'POST', 'PATCH'}:
            body = b'{}'
            supplied['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader('Cache-Control')
        finally:
            connection.close()

    def assert_no_external_operations(self):
        for mock in (self.repository, self.workspace, self.credentials):
            self.assertEqual([], mock.mock_calls)

    def test_get_uses_only_native_read_only_preflight_without_connection_claim(self):
        with patch('ariadne.native_powershell.diagnostics', side_effect=[
            {**STATUS, 'classicOutlookRegistered': True}, dict(STATUS)
        ]) as diagnose:
            code, result, cache = self.request()
        self.assertEqual(200, code)
        self.assertEqual('no-store', cache)
        self.assertEqual({'mail', 'teams'}, set(result))
        self.assertTrue(result['mail']['ready'])
        self.assertFalse(result['mail']['accountVerified'])
        self.assertFalse(result['teams']['accountVerified'])
        self.assertFalse(result['mail']['persistentPolicyChanged'])
        self.assertEqual('Restricted', result['teams']['effectivePolicy'])
        self.assertEqual('RemoteSigned', result['teams']['processPolicyRequested'])
        self.assertEqual([
            ((local_mail.SCRIPT,), {'require_outlook': True}),
            ((Path(teams_uia.__file__).parent / 'windows/teams.ps1',), {}),
        ], [(call.args, call.kwargs) for call in diagnose.call_args_list])
        self.assert_no_external_operations()

    def test_all_response_fields_are_allowlisted_and_private_values_redacted(self):
        private = 'private@example.test C:/private/source.ps1 sk-private stderr-body'
        unsafe = {**STATUS, 'ready': True, 'classicOutlookRegistered': True,
                  'host': private, 'version': private, 'effectivePolicy': private,
                  'groupPolicy': private, 'processPolicyRequested': private,
                  'codes': [private], 'scopes': {'MachinePolicy': private},
                  'stderr': private, 'mailbox': private, 'credential': private,
                  'path': private, 'message': private,
                  'accountVerified': True, 'persistentPolicyChanged': True}
        with patch('ariadne.native_powershell.diagnostics', return_value=unsafe):
            code, result, _ = self.request()
        self.assertEqual(200, code)
        rendered = json.dumps(result)
        for word in ('private@example.test', 'C:/private', 'sk-private', 'stderr-body'):
            self.assertNotIn(word, rendered)
        for item in result.values():
            self.assertFalse(item['ready'])
            self.assertFalse(item['accountVerified'])
            self.assertFalse(item['persistentPolicyChanged'])
            self.assertEqual(['POWERSHELL_DIAGNOSTICS_UNAVAILABLE'], item['codes'])
        self.assert_no_external_operations()

    def test_outlook_missing_and_policy_blocked_are_independent_reasons(self):
        mail = {**STATUS, 'ready': False, 'classicOutlookRegistered': False,
                'effectivePolicy': 'AllSigned', 'processPolicyRequested': None,
                'codes': ['POWERSHELL_POLICY_BLOCKED', 'CLASSIC_OUTLOOK_MISSING']}
        teams = {**STATUS, 'ready': False, 'codes': ['POWERSHELL_POLICY_BLOCKED']}
        with patch('ariadne.native_powershell.diagnostics', side_effect=[mail, teams]):
            code, result, _ = self.request()
        self.assertEqual(200, code)
        self.assertFalse(result['mail']['ready'])
        self.assertFalse(result['mail']['classicOutlookRegistered'])
        self.assertEqual(mail['codes'], result['mail']['codes'])
        self.assertEqual(teams['codes'], result['teams']['codes'])
        self.assert_no_external_operations()

    def test_exception_details_never_reach_browser(self):
        with patch('ariadne.native_powershell.diagnostics', side_effect=RuntimeError(
            'sk-private private@example.test C:/private/source.ps1 stderr-body'
        )):
            code, result, _ = self.request()
        self.assertEqual(500, code)
        self.assertEqual('INTERNAL_ERROR', result['error']['code'])
        rendered = json.dumps(result)
        for word in ('sk-private', 'private@example.test', 'C:/private', 'stderr-body'):
            self.assertNotIn(word, rendered)
        self.assert_no_external_operations()

    def test_cross_origin_invalid_query_and_mutation_methods_never_run_diagnosis(self):
        with patch('ariadne.native_powershell.diagnostics') as diagnose:
            self.assertEqual(403, self.request(headers={'Origin': 'https://foreign.example.test'})[0])
            self.assertEqual(403, self.request(headers={'Sec-Fetch-Site': 'cross-site'})[0])
            self.assertEqual(403, self.request(headers={'Host': 'foreign.example.test'})[0])
            self.assertEqual(400, self.request(path=PATH+'?mailbox=private')[0])
            self.assertEqual(404, self.request(method='POST')[0])
            self.assertEqual(404, self.request(method='PATCH')[0])
            diagnose.assert_not_called()
        self.assert_no_external_operations()

    def test_read_only_preflight_requires_no_registered_workspace_or_account(self):
        isolated = create_server(Mock(), port=0)
        thread = threading.Thread(target=isolated.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection('127.0.0.1', isolated.server_address[1], timeout=5)
        try:
            with patch('ariadne.native_powershell.diagnostics', return_value={
                'ready': False, 'codes': ['WINDOWS_REQUIRED'],
            }) as diagnose:
                connection.request('GET', PATH)
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                data = json.loads(response.read())
                self.assertFalse(data['mail']['ready'])
                self.assertFalse(data['mail']['accountVerified'])
                self.assertEqual(['WINDOWS_REQUIRED'], data['teams']['codes'])
                self.assertEqual(2, diagnose.call_count)
        finally:
            connection.close()
            isolated.shutdown()
            isolated.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
