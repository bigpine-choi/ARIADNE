"""Mail archive HTTP contracts with a trusted synthetic capture boundary."""

from contextlib import redirect_stderr
from copy import deepcopy
from http.client import HTTPConnection
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ariadne.demo import seed_demo
from ariadne.outlook.errors import OutlookError
from ariadne.repository import Repository
from ariadne.server import create_server


def capture():
    return {
        "mailbox": {"id": "1" * 64, "username": "synthetic@example.invalid"},
        "folder": "inbox", "fetchedAt": "2026-10-01T00:00:00Z",
        "messages": [{
            "id": "AAMk_synthetic-message", "internetMessageId": "<synthetic@example.invalid>",
            "subject": "[가상] 검토 요청", "fromName": "가상 발신자",
            "fromAddress": "sender@example.invalid",
            "toRecipients": [{"name": "가상 수신자", "address": "synthetic@example.invalid"}],
            "recipientsTruncated": False, "receivedAt": "2026-09-30T01:00:00Z",
            "sentAt": "2026-09-30T00:59:00Z", "bodyText": "검토 요청 원문입니다.",
            "bodyTruncated": False, "hasAttachments": True,
            "conversationId": "synthetic-thread", "lastModifiedAt": "2026-09-30T01:00:00Z",
            "changeKey": "synthetic-version-1", "isDraft": False,
            "attachments": [{"id": "synthetic-attachment", "name": "가상 자료.pdf",
                "contentType": "application/pdf", "size": 1234, "isInline": False,
                "kind": "file", "lastModifiedAt": "2026-09-30T01:00:00Z",
                "state": "not_downloaded"}], "attachmentsTruncated": False,
        }],
    }


class TrustedSyntheticOutlook:
    """Never contacts a provider; content comes from this fixture, not the client."""
    secret = "synthetic-provider-token-must-not-leak"

    def __init__(self):
        self.snapshot = capture()
        self.calls = 0
        self.failure = None

    def status(self):
        return {"state": "connected"}

    def import_messages(self, preview_id, message_ids, save_callback):
        self.calls += 1
        if self.failure:
            raise self.failure
        if preview_id != "synthetic-preview":
            raise OutlookError("OUTLOOK_PREVIEW_EXPIRED", "미리보기를 다시 조회해 주세요.", 410)
        if message_ids != [self.snapshot["messages"][0]["id"]]:
            raise OutlookError("OUTLOOK_SELECTION_INVALID", "선택한 메일을 확인해 주세요.", 400)
        return save_callback(deepcopy(self.snapshot))


class MailHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.directory.name) / "mail-http.sqlite3")
        seed_demo(self.repository)
        self.outlook = TrustedSyntheticOutlook()
        self.server = create_server(self.repository, port=0, outlook=self.outlook)
        self.port = self.server.server_address[1]
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.repository.close()
        self.directory.cleanup()

    def request(self, method, path, payload=None, headers=None, raw=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = raw if raw is not None else None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        logs = StringIO()
        try:
            with redirect_stderr(logs):
                connection.request(method, path, body, {"Content-Type": "application/json", **(headers or {})})
                response = connection.getresponse()
                data = response.read()
            self.assertEqual("", logs.getvalue())
            self.assertNotIn(self.outlook.secret.encode(), data)
            self.assertEqual("no-store", response.getheader("Cache-Control"))
            return response.status, json.loads(data) if response.getheader("Content-Type").startswith("application/json") else data, dict(response.getheaders())
        finally:
            connection.close()

    def import_payload(self):
        return {"previewId": "synthetic-preview", "messageIds": ["AAMk_synthetic-message"], "actorId": "가상 검토자"}

    def imported(self):
        status, result, _ = self.request("POST", "/api/mail/import", self.import_payload())
        self.assertEqual(200, status, result)
        self.assertEqual(1, result["created"])
        return result["items"][0]

    def test_capture_search_detail_and_duplicate_preserve_work(self):
        before = self.repository.list_work_items()
        saved = self.imported()
        audit_count = len(self.repository.audit())
        status, duplicate, _ = self.request("POST", "/api/mail/import", self.import_payload())
        self.assertEqual(200, status)
        self.assertEqual((0, 1, 0), (duplicate["created"], duplicate["unchanged"], duplicate["revised"]))
        self.assertEqual(audit_count, len(self.repository.audit()))
        status, found, _ = self.request("POST", "/api/mail/search", {"query": "검토 요청", "limit": 10, "offset": 0})
        self.assertEqual(200, status)
        self.assertEqual(1, found["total"])
        self.assertEqual(saved["mailId"], found["items"][0]["id"])
        status, detail, _ = self.request("GET", "/api/mail/" + saved["mailId"])
        self.assertEqual(200, status)
        self.assertEqual("검토 요청 원문입니다.", detail["mail"]["message"]["bodyText"])
        self.assertEqual("not_downloaded", detail["mail"]["message"]["attachments"][0]["state"])
        self.assertEqual(before, self.repository.list_work_items())

    def test_explicit_link_is_additive_idempotent_and_never_completes_work(self):
        saved = self.imported()
        work = self.repository.list_work_items()[0]
        payload = {"revision": 1, "workItemId": work["id"], "expectedVersion": work["version"],
                   "actorId": "가상 검토자", "reason": "요청 메일 근거 연결"}
        status, linked, _ = self.request("POST", f'/api/mail/{saved["mailId"]}/link', payload)
        self.assertEqual(200, status, linked)
        self.assertFalse(linked["alreadyLinked"])
        self.assertEqual([*work["sources"], saved["evidenceId"]], linked["item"]["sources"])
        for field in ("title", "status", "due", "completionEvidence", "ownerId"):
            self.assertEqual(work[field], linked["item"][field])
        count = len(self.repository.audit())
        payload["expectedVersion"] = linked["item"]["version"]
        status, repeated, _ = self.request("POST", f'/api/mail/{saved["mailId"]}/link', payload)
        self.assertEqual(200, status)
        self.assertTrue(repeated["alreadyLinked"])
        self.assertEqual(count, len(self.repository.audit()))
        self.assertEqual(linked["item"]["version"], repeated["item"]["version"])

    def test_stale_work_version_cannot_attach_evidence(self):
        saved = self.imported()
        work = self.repository.list_work_items()[0]
        updated = self.repository.update_work_item(work["id"], {"title": "사용자가 확정한 제목"}, work["version"], "사용자", "직접 수정")
        status, _, _ = self.request("POST", f'/api/mail/{saved["mailId"]}/link', {
            "revision": 1, "workItemId": work["id"], "expectedVersion": work["version"], "actorId": "검토자", "reason": "연결"})
        self.assertEqual(409, status)
        self.assertEqual(updated, self.repository.get_work_item(work["id"]))

    def test_old_revision_is_readable_after_provider_change(self):
        saved = self.imported()
        self.outlook.snapshot["messages"][0].update(bodyText="변경된 요청", changeKey="synthetic-version-2")
        status, result, _ = self.request("POST", "/api/mail/import", self.import_payload())
        self.assertEqual(200, status)
        self.assertEqual(1, result["revised"])
        status, old, _ = self.request("GET", f'/api/mail/{saved["mailId"]}/revisions/1')
        self.assertEqual(200, status)
        self.assertEqual(saved["evidenceId"], old["mail"]["evidenceId"])
        self.assertEqual("검토 요청 원문입니다.", old["mail"]["message"]["bodyText"])

    def test_browser_cannot_supply_message_content_or_account_identity(self):
        for field, value in (("messages", capture()["messages"]), ("mailbox", capture()["mailbox"]), ("bodyText", "위조")):
            with self.subTest(field=field):
                status, _, _ = self.request("POST", "/api/mail/import", {**self.import_payload(), field: value})
                self.assertEqual(400, status)
        self.assertEqual(0, self.outlook.calls)
        self.assertEqual(0, self.repository.mail.search()["total"])

    def test_invalid_selection_and_automated_actor_do_not_call_provider(self):
        payloads = [{**self.import_payload(), "messageIds": ids} for ids in ([], ["x", "x"], [str(i) for i in range(11)], True)]
        payloads += [{**self.import_payload(), "actorId": actor} for actor in ("AI", "system", "", "x" * 201)]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(400, self.request("POST", "/api/mail/import", payload)[0])
        self.assertEqual(0, self.outlook.calls)

    def test_expired_or_tampered_preview_cannot_write(self):
        for payload, expected in (({**self.import_payload(), "previewId": "expired"}, 410),
                                  ({**self.import_payload(), "messageIds": ["other-account-message"]}, 400)):
            self.assertEqual(expected, self.request("POST", "/api/mail/import", payload)[0])
        self.assertEqual(0, self.repository.mail.search()["total"])

    def test_generic_evidence_route_cannot_forge_outlook_capture(self):
        forged = {"id": "forged", "provider": "outlook", "sourceId": "unverified", "capturedAt": "2026-10-01T00:00:00Z",
                  "evidence": "위조 원문", "locator": "위조 위치", "sha256": None, "threadId": None,
                  "finality": "unknown", "role": "context"}
        status, _, _ = self.request("POST", "/api/evidence", {"evidence": forged, "actorId": "검토자"})
        self.assertEqual(400, status)
        forged.update(id="manual-proof", provider="manual")
        self.assertEqual(201, self.request("POST", "/api/evidence", {"evidence": forged, "actorId": "검토자"})[0])

    def test_origin_host_and_non_json_boundaries_cover_import_and_archive(self):
        for headers in ({"Origin": "https://foreign.invalid"}, {"Host": "foreign.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(403, self.request("POST", "/api/mail/import", self.import_payload(), headers)[0])
            self.assertEqual(403, self.request("GET", "/api/mail", headers=headers)[0])
        self.assertEqual(400, self.request("POST", "/api/mail/import", self.import_payload(), {"Content-Type": "text/plain"})[0])
        self.assertEqual(0, self.outlook.calls)

    def test_strict_methods_json_and_search_limits(self):
        for method, path in (("GET", "/api/mail/import"), ("GET", "/api/mail/search"), ("DELETE", "/api/mail"), ("POST", "/api/mail/x")):
            self.assertEqual(405, self.request(method, path, {} if method == "POST" else None)[0])
        self.assertEqual(400, self.request("POST", "/api/mail/import", raw=b'{"previewId":"a","previewId":"b"}')[0])
        for query in ({"query": "", "limit": True, "offset": 0}, {"query": "", "limit": 51, "offset": 0},
                      {"query": [], "limit": 10, "offset": 0}, {"query": "", "limit": 10, "offset": -1}):
            self.assertEqual(400, self.request("POST", "/api/mail/search", query)[0])
        self.assertEqual(404, self.request("GET", "/api/mail/nonexistent")[0])

    def test_unexpected_provider_error_is_sanitized_and_does_not_write(self):
        self.outlook.failure = RuntimeError(self.outlook.secret)
        status, result, _ = self.request("POST", "/api/mail/import", self.import_payload())
        self.assertEqual(500, status)
        self.assertEqual("INTERNAL_ERROR", result["error"]["code"])
        self.assertEqual(0, self.repository.mail.search()["total"])

    def test_mail_asset_uses_existing_no_cache_csp_boundary(self):
        status, asset, headers = self.request("GET", "/assets/mail.js")
        self.assertEqual(200, status)
        self.assertGreater(len(asset), 100)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
