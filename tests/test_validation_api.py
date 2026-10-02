"""HTTP approval, origin and immutable-report contract for Omen validation."""

import http.client
import hashlib
import json
import threading
import unittest
from unittest.mock import Mock

from ariadne.credentials import CredentialError
from ariadne.models import ValidationError
from ariadne.repository import ConflictError, NotFoundError
from ariadne.server import create_server


RUN_ID = "2026-10-02-" + "a" * 12 + "-validation-" + "b" * 32
OUTPUT_HASH = "c" * 64
BASE = "/api/workspace/validation/"
PREPARED = {
    "id": RUN_ID,
    "status": "prepared",
    "outputVersionId": 19,
    "outputHash": OUTPUT_HASH,
    "period": {"start": "2026-09-25", "end": "2026-10-02"},
    "recipient": "owner@example.test",
    "warnings": ["일부 자료는 화면 수집 범위로 제한됩니다."],
    "limitations": ["첨부파일 전체 본문은 수집되지 않습니다."],
}


class ValidationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = Mock()
        cls.archive = Mock()
        cls.server = create_server(Mock(), port=0, workspace=cls.workspace,
                                   automation_store=cls.archive)
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.workspace.reset_mock(return_value=True, side_effect=True)
        self.archive.reset_mock(return_value=True, side_effect=True)
        self.workspace.prepare_validation.return_value = dict(PREPARED)
        self.workspace.send_validation.return_value = {**PREPARED, "status": "submitted"}
        self.workspace.confirm_validation.return_value = {
            **PREPARED, "status": "delivered", "receivedAt": "2026-10-02T10:00:00+09:00"
        }
        self.archive.runs.return_value = [{
            "id": RUN_ID, "detail": json.dumps({"outputVersionId": 19})
        }]
        self.archive.read_version.return_value = b"archived-xlsx-bytes"

    def request(self, action, payload=None, *, headers=None, raw=None, method="POST"):
        body = raw if raw is not None else (
            json.dumps(payload).encode("utf-8") if payload is not None else None
        )
        supplied = {"Content-Type": "application/json", "Origin": self.origin}
        supplied.update(headers or {})
        path = action if action.startswith("/") else BASE + action
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            data = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, data
        finally:
            connection.close()

    def assert_no_validation_operation(self):
        self.workspace.prepare_validation.assert_not_called()
        self.workspace.send_validation.assert_not_called()
        self.workspace.confirm_validation.assert_not_called()

    def test_prepare_generates_without_any_send_or_delivery(self):
        code, result = self.request("prepare", {"scopeAcknowledged": False})
        self.assertEqual(200, code)
        self.assertEqual(PREPARED, result)
        self.workspace.prepare_validation.assert_called_once_with(scope_acknowledged=False)
        self.workspace.send_validation.assert_not_called()
        self.workspace.confirm_validation.assert_not_called()

    def test_scope_acknowledgement_is_explicit_and_validation_only(self):
        code, _ = self.request("prepare", {"scopeAcknowledged": True})
        self.assertEqual(200, code)
        self.workspace.prepare_validation.assert_called_once_with(scope_acknowledged=True)
        self.workspace.save_settings.assert_not_called()
        self.workspace.start.assert_not_called()
        self.workspace.send_validation.assert_not_called()

    def test_invalid_prepare_scope_or_extra_actions_are_rejected(self):
        for payload in ({}, {"scopeAcknowledged": 1}, {"scopeAcknowledged": "true"},
                        {"scopeAcknowledged": None},
                        {"scopeAcknowledged": True, "autoSend": True},
                        {"scopeAcknowledged": True, "model": "override"}):
            with self.subTest(payload=payload):
                self.assertEqual(400, self.request("prepare", payload)[0])
        self.assert_no_validation_operation()

    def test_send_requires_literal_approval_and_exact_report_digest(self):
        code, result = self.request("send", {
            "id": RUN_ID, "outputHash": OUTPUT_HASH, "approval": True
        })
        self.assertEqual(200, code)
        self.assertEqual("submitted", result["status"])
        self.workspace.send_validation.assert_called_once_with(RUN_ID, OUTPUT_HASH)
        self.workspace.prepare_validation.assert_not_called()
        self.workspace.confirm_validation.assert_not_called()

    def test_missing_false_and_nonboolean_send_approval_are_rejected(self):
        for approval in (False, None, 1, "true"):
            with self.subTest(approval=approval):
                self.assertEqual(400, self.request("send", {
                    "id": RUN_ID, "outputHash": OUTPUT_HASH, "approval": approval
                })[0])
        self.assertEqual(400, self.request("send", {
            "id": RUN_ID, "outputHash": OUTPUT_HASH
        })[0])
        self.workspace.send_validation.assert_not_called()

    def test_invalid_digest_identifier_or_arbitrary_recipient_are_rejected(self):
        valid = {"id": RUN_ID, "outputHash": OUTPUT_HASH, "approval": True}
        invalid = [
            {**valid, "outputHash": "c" * 63},
            {**valid, "outputHash": "C" * 64},
            {**valid, "outputHash": ["c" * 64]},
            {**valid, "id": "../outside-validation-id"},
            {**valid, "id": "2026-10-02-" + "a" * 12},
            {**valid, "id": "x-validation-" + "a" * 121},
            {**valid, "recipient": "other@example.test"},
            {**valid, "attachment": "C:/private/report.xlsx"},
            {**valid, "model": "override"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(400, self.request("send", payload)[0])
        self.workspace.send_validation.assert_not_called()

    def test_duplicate_approval_key_is_rejected_without_sending(self):
        raw = ('{"id":"' + RUN_ID + '","outputHash":"' + OUTPUT_HASH +
               '","approval":false,"approval":true}').encode()
        code, _ = self.request("send", raw=raw)
        self.assertEqual(400, code)
        self.workspace.send_validation.assert_not_called()

    def test_delivery_is_only_a_read_and_preserves_result_fields(self):
        code, result = self.request("delivery", {"id": RUN_ID})
        self.assertEqual(200, code)
        self.assertEqual("delivered", result["status"])
        self.assertEqual(OUTPUT_HASH, result["outputHash"])
        self.assertEqual("2026-10-02T10:00:00+09:00", result["receivedAt"])
        self.workspace.confirm_validation.assert_called_once_with(RUN_ID)
        self.workspace.prepare_validation.assert_not_called()
        self.workspace.send_validation.assert_not_called()

    def test_delivery_cannot_receive_a_send_instruction(self):
        code, _ = self.request("delivery", {"id": RUN_ID, "approval": True})
        self.assertEqual(400, code)
        self.assert_no_validation_operation()

    def test_same_origin_localhost_is_supported(self):
        code, _ = self.request("prepare", {"scopeAcknowledged": False}, headers={
            "Origin": f"http://localhost:{self.port}",
            "Host": f"localhost:{self.port}",
        })
        self.assertEqual(200, code)

    def test_foreign_origin_is_rejected_for_every_operation(self):
        operations = (
            ("prepare", {"scopeAcknowledged": True}),
            ("send", {"id": RUN_ID, "outputHash": OUTPUT_HASH, "approval": True}),
            ("delivery", {"id": RUN_ID}),
        )
        for action, payload in operations:
            with self.subTest(action=action):
                code, result = self.request(action, payload, headers={
                    "Origin": "https://foreign.example.test"
                })
                self.assertEqual(403, code)
                self.assertEqual("ORIGIN_DENIED", result["error"]["code"])
        self.assert_no_validation_operation()

    def test_nonlocal_host_and_cross_site_requests_are_rejected(self):
        for headers in ({"Host": "foreign.example.test"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(403, self.request("prepare", {
                    "scopeAcknowledged": True
                }, headers=headers)[0])
        self.assert_no_validation_operation()

    def test_changed_report_digest_and_run_conflicts_are_sanitized(self):
        self.workspace.send_validation.side_effect = ConflictError(
            "token-secret C:/private/source.msg mailbox-detail"
        )
        code, result = self.request("send", {
            "id": RUN_ID, "outputHash": OUTPUT_HASH, "approval": True
        })
        self.assertEqual(409, code)
        self.assertEqual("CONFLICT", result["error"]["code"])
        self.assertNotIn("token-secret", json.dumps(result))
        self.assertNotIn("C:/private", json.dumps(result))

    def test_credentials_validation_and_unexpected_errors_do_not_leak(self):
        for error, expected in (
            (CredentialError("token-secret"), 400),
            (ValidationError("private-mail-body"), 400),
            (ValueError("C:/private/report.xlsx"), 400),
            (RuntimeError("token-secret private-mail-body"), 500),
            (NotFoundError("private-mail-body"), 404),
        ):
            with self.subTest(error=type(error).__name__):
                self.workspace.confirm_validation.side_effect = error
                code, result = self.request("delivery", {"id": RUN_ID})
                self.assertEqual(expected, code)
                rendered = json.dumps(result)
                for secret in ("token-secret", "private-mail-body", "C:/private"):
                    self.assertNotIn(secret, rendered)
        self.workspace.send_validation.assert_not_called()

    def test_version_pinned_download_returns_the_archived_artifact(self):
        code, payload = self.request(
            f"/api/reports/{RUN_ID}/versions/19/download", method="GET"
        )
        self.assertEqual(200, code)
        self.assertEqual(b"archived-xlsx-bytes", payload)
        self.archive.read_version.assert_called_once_with(19)
        self.assert_no_validation_operation()

    def test_version_pinned_download_rejects_a_different_version(self):
        code, _ = self.request(
            f"/api/reports/{RUN_ID}/versions/20/download", method="GET"
        )
        self.assertEqual(404, code)
        self.archive.read_version.assert_not_called()

    def test_previous_report_download_route_is_preserved(self):
        code, payload = self.request(f"/api/reports/{RUN_ID}/download", method="GET")
        self.assertEqual(200, code)
        self.assertEqual(b"archived-xlsx-bytes", payload)
        self.archive.read_version.assert_called_once_with(19)

    def archived_validation(self, status='prepared'):
        owner='owner@example.test'
        identifier=('2026-10-02-'+hashlib.sha256(owner.encode()).hexdigest()[:12]
                    +'-validation-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
        content=b'archived-xlsx-bytes'
        detail={'kind':'operational_validation','recipient':owner,'outputVersionId':19,
                'outputHash':hashlib.sha256(content).hexdigest(),
                'warnings':['검토 필요'],'limitations':['화면 수집 범위 제한'],
                'sendingStartedAt':'private-internal-value','secret':'token-secret'}
        self.workspace.settings.return_value={'mailbox':owner,'device_role':'omen'}
        return {'id':identifier,'start':'2026-09-25','end':'2026-10-02','status':status,
                'output':'C:/private/report.xlsx','detail':json.dumps(detail)}

    def test_archived_prepared_run_can_be_restored_without_approval_or_send(self):
        run=self.archived_validation()
        self.archive.runs.return_value=[run]
        code,result=self.request('runs',method='GET')
        self.assertEqual(200,code)
        self.assertEqual(1,len(result['items']))
        self.assertEqual(run['id'],result['items'][0]['id'])
        self.assertEqual('prepared',result['items'][0]['status'])
        self.assertEqual(19,result['items'][0]['outputVersionId'])
        self.assertEqual({'start':run['start'],'end':run['end']},result['items'][0]['period'])
        self.assertNotIn('approvedAt',result['items'][0])
        for secret in ('token-secret','C:/private','private-internal-value'):
            self.assertNotIn(secret,json.dumps(result))
        self.assert_no_validation_operation()

    def test_unknown_submission_survives_a_new_detail_request(self):
        run=self.archived_validation('delivery_unknown')
        self.archive.runs.return_value=[run]
        code,result=self.request('runs/'+run['id'],method='GET')
        self.assertEqual(200,code)
        self.assertEqual('delivery_unknown',result['run']['status'])
        self.assertEqual('delivery_unknown',result['run']['runStatus'])
        self.assertEqual(hashlib.sha256(b'archived-xlsx-bytes').hexdigest(),result['run']['outputHash'])
        self.assert_no_validation_operation()

    def test_run_restore_filters_owner_type_identity_and_missing_artifact(self):
        run=self.archived_validation()
        original=json.loads(run['detail'])
        invalid=[
            {**run,'detail':json.dumps({**original,'recipient':'other@example.test'})},
            {**run,'detail':json.dumps({**original,'kind':'weekly_preview'})},
            {**run,'id':'2026-10-02-'+'a'*12+'-validation-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'},
            {**run,'id':run['id'].split('-validation-')[0]},
            {**run,'detail':json.dumps({**original,'outputVersionId':True})},
            {**run,'detail':json.dumps({**original,'outputHash':'c'*64})},
            {**run,'detail':'private malformed JSON'},
        ]
        self.archive.runs.return_value=invalid
        code,result=self.request('runs',method='GET')
        self.assertEqual(200,code)
        self.assertEqual([],result['items'])
        self.assertEqual(404,self.request('runs/'+run['id'],method='GET')[0])
        self.assert_no_validation_operation()

    def test_changed_archived_bytes_are_not_restored(self):
        run=self.archived_validation()
        self.archive.runs.return_value=[run]
        self.archive.read_version.return_value=b'changed-bytes'
        self.assertEqual([],self.request('runs',method='GET')[1]['items'])
        self.assertEqual(404,self.request('runs/'+run['id'],method='GET')[0])
        self.assert_no_validation_operation()

    def test_restore_get_has_the_same_origin_restrictions(self):
        run=self.archived_validation()
        self.archive.runs.return_value=[run]
        for path in ('runs','runs/'+run['id']):
            self.assertEqual(403,self.request(path,method='GET',headers={
                'Origin':'https://foreign.example.test'
            })[0])
        self.archive.runs.assert_not_called()
        self.assert_no_validation_operation()

    def test_company_relay_and_changed_mailbox_cannot_restore_old_runs(self):
        run=self.archived_validation()
        self.archive.runs.return_value=[run]
        for settings in ({'mailbox':'owner@example.test','device_role':'company-relay'},
                         {'mailbox':'other@example.test','device_role':'omen'}):
            self.workspace.settings.return_value=settings
            self.assertEqual([],self.request('runs',method='GET')[1]['items'])
        self.assert_no_validation_operation()


if __name__ == "__main__":
    unittest.main()
