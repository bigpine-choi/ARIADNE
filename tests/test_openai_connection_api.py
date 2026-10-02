"""The explicit model check saves only model settings and never returns keys."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ariadne.repository import Repository
from ariadne.server import create_server


class SettingsWorkspace:
    def __init__(self):
        self.lock = threading.RLock()
        self.value = {'model': '', 'folder': 'synthetic-folder', 'enabled': False,
                      'auto_send': False, 'collection_enabled': False}

    def settings(self):
        return deepcopy(self.value)

    def save_settings(self, value):
        self.value = deepcopy(value)
        return self.settings()


class ConnectionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name) / 'work.sqlite3')
        self.vault = Mock()
        self.vault.status.return_value = {'saved': False, 'connectionVerified': False}
        self.workspace = SettingsWorkspace()
        self.server = create_server(self.repo, port=0, credential_store=self.vault, workspace=self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.repo.close()
        self.tmp.cleanup()

    def request(self, path, body=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with closing(error):
                return error.code, json.load(error)

    def test_explicit_check_saves_only_model_and_returns_sanitized_result(self):
        before = self.workspace.settings()
        expected = {'ok': True, 'code': 'CONNECTED', 'model': 'configured-model',
                    'message': 'model lookup verified', 'checkedAt': '2026-10-02T00:00:00+00:00',
                    'scope': 'authentication_model_lookup'}
        with patch('ariadne.openai_connection.check_connection', return_value=expected) as check:
            status, result = self.request('/api/settings/openai/check', {'model': ' configured-model '})
        self.assertEqual((200, expected), (status, result))
        check.assert_called_once_with('configured-model', self.vault)
        self.assertEqual({**before, 'model': 'configured-model'}, self.workspace.settings())
        self.vault.save.assert_not_called()
        self.vault.remove.assert_not_called()

    def test_page_status_never_decrypts_or_contacts_openai(self):
        with patch('ariadne.openai_connection.check_connection') as check:
            status, result = self.request('/api/settings/openai')
        self.assertEqual(200, status)
        self.assertFalse(result['connectionVerified'])
        check.assert_not_called()
        self.vault.read.assert_not_called()

    def test_invalid_model_and_extra_credentials_fail_without_settings_write(self):
        before = self.workspace.settings()
        for body in ({'model': ''}, {'model': 'https://example.invalid/model'},
                     {'model': 'configured-model', 'apiKey': 'synthetic-never-echo-credential'}):
            with self.subTest(body=body), patch('ariadne.openai_connection.check_connection') as check:
                status, result = self.request('/api/settings/openai/check', body)
                self.assertEqual(400, status)
                self.assertNotIn('synthetic-never-echo-credential', json.dumps(result))
                check.assert_not_called()
        self.assertEqual(before, self.workspace.settings())

    def test_cross_origin_probe_is_blocked_without_settings_write(self):
        before = self.workspace.settings()
        with patch('ariadne.openai_connection.check_connection') as check:
            status, _ = self.request('/api/settings/openai/check', {'model': 'configured-model'},
                                     'https://example.invalid')
        self.assertEqual(403, status)
        self.assertEqual(before, self.workspace.settings())
        check.assert_not_called()


if __name__ == '__main__':
    unittest.main()
