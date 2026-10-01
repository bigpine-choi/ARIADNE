"""Local collector boundaries: synthetic provider, real archive and HTTP."""

from base64 import b64decode
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from ariadne.demo import seed_demo
from ariadne.models import ValidationError
from ariadne.outlook.errors import OutlookError
from ariadne.outlook.local import LocalOutlookService, PowerShellOutlook
from ariadne.repository import Repository
from ariadne.server import create_server
from test_mail_http import capture


class FakeLocal:
    supported = True

    def __init__(self):
        self.calls = []
        self.messages = capture()["messages"]
        self.failed = 0
        self.truncated = False
        self.failure = None
        self.gate = None
        self.started = Event()

    def run(self, payload):
        self.calls.append(deepcopy(payload))
        self.started.set()
        if self.gate:
            self.gate.wait(3)
        if self.failure:
            raise self.failure
        if payload["operation"] == "discover":
            return {"ok": True, "warnings": 1, "truncated": False, "folders": [
                {"storeId": "STORE-A", "folderId": "FOLDER-A", "storeName": "가상 사서함",
                 "folderPath": r"\\가상 사서함\받은 편지함\펀드 A"},
                {"storeId": "STORE-B", "folderId": "FOLDER-B", "storeName": "가상 사서함",
                 "folderPath": r"\\가상 사서함\보낸 편지함"}]}
        return {"ok": True, "messages": deepcopy(self.messages), "failed": self.failed,
                "truncated": self.truncated}


class LocalServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "local.sqlite3"
        self.repo = Repository(self.path)
        seed_demo(self.repo)
        self.adapter = FakeLocal()
        self.now = 100
        self.service = LocalOutlookService(self.repo, self.adapter, clock=lambda: self.now)
        self.catalog = self.finish(self.service.start("discover", "discovery"))
        self.query = {"folderKey": self.catalog["folders"][0]["key"], "basis": "inbox",
                      "startDate": "2026-09-30", "endDate": "2026-10-01", "limit": 10,
                      "actorId": "가상 사용자"}

    def tearDown(self):
        self.service.close()
        if self.service.worker:
            self.service.worker.join(4)
        self.repo.close()
        self.directory.cleanup()

    def finish(self, job):
        self.service.worker.join(4)
        self.assertFalse(self.service.worker.is_alive())
        return self.service.job(job["id"])

    def collect(self, request_id="collect", **changes):
        return self.finish(self.service.start("collect", request_id, **{**self.query, **changes}))

    def test_discovery_uses_opaque_keys_and_separate_same_name_accounts(self):
        self.assertEqual(2, len(self.catalog["folders"]))
        self.assertEqual(1, self.catalog["warnings"])
        self.assertNotEqual(*[f["accountKey"] for f in self.catalog["folders"]])
        self.assertNotIn("STORE-A", json.dumps(self.catalog))
        self.assertNotIn("fingerprint", self.catalog)

    def test_collect_persists_source_and_deduplicates(self):
        before = self.repo.get_work_item("synthetic-task-001")
        first = self.collect()
        self.assertEqual(("completed", 1, 0), (first["state"], first["created"], first["failed"]))
        listing = self.repo.mail.search()
        detail = self.repo.mail.get(listing["items"][0]["id"])
        self.assertEqual("outlook_com", detail["message"]["localSource"]["kind"])
        self.assertTrue(detail["message"]["id"].startswith("com:"))
        self.assertEqual(1, self.collect("retry")["unchanged"])
        self.assertEqual(1, len(self.repo.mail.get(detail["id"])["revisions"]))
        self.assertEqual(before, self.repo.get_work_item("synthetic-task-001"))
        with Repository(self.path) as restored:
            self.assertEqual(1, restored.mail.search()["total"])

    def test_changed_mail_appends_and_existing_link_keeps_old_revision(self):
        self.collect()
        mail_id = self.repo.mail.search()["items"][0]["id"]
        work = self.repo.get_work_item("synthetic-task-001")
        old = self.repo.mail.get(mail_id)
        self.repo.mail.link(mail_id, 1, work["id"], work["version"], "검토자", "가상 연결")
        self.adapter.messages[0]["bodyText"] = "가상 변경 본문"
        self.adapter.messages[0]["changeKey"] = "version-two"
        self.assertEqual(1, self.collect("changed")["revised"])
        self.assertEqual(2, len(self.repo.mail.get(mail_id)["revisions"]))
        self.assertEqual(old["message"], self.repo.mail.get(mail_id, 1)["message"])
        self.assertIn(old["evidenceId"], self.repo.get_work_item(work["id"])["sources"])

    def test_local_graph_and_other_stores_are_separate(self):
        self.repo.mail.import_messages(capture(), "사용자")
        self.collect()
        self.collect("store-b", folderKey=self.catalog["folders"][1]["key"], basis="sentitems")
        self.assertEqual(3, self.repo.mail.search()["total"])

    def test_server_restart_requires_rediscovery_but_deduplicates(self):
        self.collect()
        self.service.close()
        self.service = LocalOutlookService(self.repo, self.adapter)
        with self.assertRaises(OutlookError):
            self.service.start("collect", "new", **self.query)
        fresh = self.finish(self.service.start("discover", "new-discovery"))
        self.query["folderKey"] = fresh["folders"][0]["key"]
        self.assertEqual(1, self.collect("new-collect")["unchanged"])

    def test_partial_provider_and_invalid_message_failures_are_counted(self):
        self.adapter.failed = 1
        invalid = deepcopy(self.adapter.messages[0]); invalid["id"] = "bad"; invalid["bodyText"] = None
        self.adapter.messages.append(invalid)
        result = self.collect()
        self.assertEqual(("partial", 1, 2, 3), (result["state"], result["created"], result["failed"], result["processed"]))
        self.assertEqual(1, self.repo.mail.search()["total"])

    def test_empty_and_truncated_results_are_distinct(self):
        self.adapter.messages = []
        result = self.collect()
        self.assertEqual((0, "completed", False), (result["total"], result["state"], result["truncated"]))
        self.adapter.truncated = True
        self.assertTrue(self.collect("limited")["truncated"])

    def test_timeout_and_exception_do_not_leak_provider_content(self):
        self.adapter.failure = RuntimeError("private-content-secret")
        result = self.collect()
        self.assertEqual("failed", result["state"])
        self.assertNotIn("private-content-secret", json.dumps(result))
        self.assertEqual(0, self.repo.mail.search()["total"])

    def test_receipt_expiry_and_forged_folder_rejected(self):
        with self.assertRaises(OutlookError):
            self.collect(folderKey="raw-provider-id")
        self.now += 1801
        with self.assertRaises(OutlookError):
            self.collect()
        self.assertEqual(1, len(self.adapter.calls))

    def test_invalid_range_actor_and_limit_fail_before_provider(self):
        for change in [{"startDate": "2026-10-02"}, {"startDate": "2026-08-01"},
                       {"startDate": "20260930"}, {"limit": True}, {"limit": 51},
                       {"actorId": " system "}, {"basis": "drafts"}]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.collect(**change)
        self.assertEqual(1, len(self.adapter.calls))

    def test_duplicate_request_is_idempotent_and_changed_body_conflicts(self):
        first = self.collect()
        calls = len(self.adapter.calls)
        second = self.collect()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(calls, len(self.adapter.calls))
        with self.assertRaises(OutlookError):
            self.collect(limit=5)

    def test_concurrent_collection_blocked_and_close_prevents_late_write(self):
        self.adapter.gate = Event(); self.adapter.started.clear()
        started = self.service.start("collect", "slow", **self.query)
        self.assertTrue(self.adapter.started.wait(2))
        with self.assertRaises(OutlookError):
            self.service.start("collect", "other", **self.query)
        self.service.close(); self.adapter.gate.set()
        result = self.finish(started)
        self.assertEqual("failed", result["state"])
        self.assertEqual(0, self.repo.mail.search()["total"])

    def test_invalid_provider_counts_fail_without_saving(self):
        self.adapter.failed = 50
        self.assertEqual("failed", self.collect()["state"])
        self.assertEqual(0, self.repo.mail.search()["total"])


class LocalProcessTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell execution")
    def test_real_reader_with_synthetic_outlook_objects(self):
        adapter = PowerShellOutlook()
        root = Path(__file__).parents[1]
        source = (root / "ariadne/outlook/local_reader.ps1").read_text(encoding="utf-8")
        mock = (root / "tests/fixtures/local_outlook_mock.ps1").read_text(encoding="utf-8")
        source = source.replace("$app = New-Object -ComObject Outlook.Application", "$app = $global:FakeApplication")
        from base64 import b64encode
        for culture in ("en-US", "ko-KR"):
            script = mock + "\n[Threading.Thread]::CurrentThread.CurrentCulture=[Globalization.CultureInfo]::GetCultureInfo('" + culture + "')\n" + source
            command = [str(adapter.executable), "-NoProfile", "-NonInteractive", "-Sta", "-EncodedCommand", b64encode(script.encode("utf-16-le")).decode()]
            self.assertLess(len(' '.join(command)), 32700)
            def execute(payload):
                result = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace") + result.stdout.decode(errors="replace"))
                return json.loads(result.stdout.decode("utf-8-sig"))
            catalog = execute({"operation": "discover"})
            self.assertEqual("FOLDER-A", catalog["folders"][0]["folderId"])
            for basis in ("inbox", "sentitems"):
                with self.subTest(culture=culture, basis=basis):
                    data = execute({"operation": "collect", "storeId": "STORE-A", "folderId": "FOLDER-A", "folderPath": "Synthetic/Inbox",
                                    "basis": basis, "startDate": "2026-09-30", "endDate": "2026-10-01", "limit": 1})
                    self.assertEqual(0, data["failed"])
                    self.assertTrue(data["truncated"])
                    m = data["messages"][0]
                    self.assertEqual("AQID", m["changeKey"])
                    self.assertEqual("Synthetic body", m["bodyText"])
                    self.assertEqual("synthetic.pdf", m["attachments"][0]["name"])
                    self.assertFalse(m["attachments"][0]["isInline"])

    def test_windows_process_uses_fixed_sta_code_and_json_stdin(self):
        adapter = PowerShellOutlook()
        payload = {"operation": "collect", "folderId": "x';Remove-Item forbidden;#"}
        with patch.object(PowerShellOutlook, "supported", True), patch("ariadne.outlook.local.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b'{"ok":true,"messages":[]}', b'')
            adapter.run(payload)
            args, options = run.call_args
            self.assertIn("-Sta", args[0])
            self.assertNotIn("-ExecutionPolicy", args[0])
            script = b64decode(args[0][-1]).decode("utf-16-le")
            self.assertNotIn("Remove-Item", script)
            self.assertEqual(payload, json.loads(options["input"]))
            self.assertEqual(180, options["timeout"])
            self.assertFalse(options.get("shell", False))
            self.assertLess(len(' '.join(args[0])), 32000)

    def test_process_failures_are_sanitized(self):
        adapter = PowerShellOutlook()
        for failure in [subprocess.TimeoutExpired("private", 180), OSError("private")]:
            with patch.object(PowerShellOutlook, "supported", True), patch("ariadne.outlook.local.subprocess.run", side_effect=failure):
                with self.assertRaises(OutlookError) as caught:
                    adapter.run({"operation": "discover"})
                self.assertNotIn("private", str(caught.exception))

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell parser")
    def test_packaged_powershell_parses_on_windows(self):
        adapter = PowerShellOutlook()
        script = Path(__file__).parents[1] / "ariadne/outlook/local_reader.ps1"
        command = "$e=$null;$t=$null;[System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$t,[ref]$e)|Out-Null;if($e.Count){$e|Out-String|Write-Output;exit 1}"
        # ParseFile via a literal path passed as input data avoids code quoting.
        command = command.replace('$args[0]', '[Console]::In.ReadToEnd()')
        result = subprocess.run([str(adapter.executable), "-NoProfile", "-Command", command], input=str(script), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class LocalHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.repo = Repository(Path(self.directory.name)/"http.sqlite3")
        self.service = LocalOutlookService(self.repo, FakeLocal())
        self.server = create_server(self.repo, port=0, local_outlook=self.service)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        if self.service.worker: self.service.worker.join(4)
        self.repo.close(); self.directory.cleanup()

    def call(self, path, body=None, headers=None, method=None):
        c = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        c.request(method or ("POST" if body is not None else "GET"), path,
                  None if body is None else json.dumps(body), {"Content-Type": "application/json", **(headers or {})})
        r=c.getresponse(); data=r.read(); status=r.status; c.close()
        return status, json.loads(data)

    def test_collect_roundtrip_uses_receipt_and_archive(self):
        status, job = self.call('/api/local-outlook/discover', {"requestId":"discover"})
        self.assertEqual(202, status); self.service.worker.join(3)
        status, job = self.call('/api/local-outlook/jobs/'+job['id'])
        body = {"requestId":"collect", "folderKey":job['folders'][0]['key'], "basis":"inbox",
                "startDate":"2026-09-30", "endDate":"2026-10-01", "limit":5, "actorId":"가상 사용자"}
        status, job = self.call('/api/local-outlook/collect', body)
        self.assertEqual(202, status); self.service.worker.join(3)
        self.assertEqual(1, self.call('/api/local-outlook/jobs/'+job['id'])[1]['created'])
        self.assertEqual(1, self.call('/api/mail')[1]['total'])

    def test_origin_methods_and_extra_fields_rejected(self):
        for path in ['/api/local-outlook/discover','/api/local-outlook/collect']:
            self.assertEqual(403, self.call(path, {}, {'Origin':'https://evil.invalid'})[0])
            self.assertEqual(405, self.call(path)[0])
        self.assertEqual(400, self.call('/api/local-outlook/discover', {'requestId':'x','bodyText':'forged'})[0])
        self.assertEqual(404, self.call('/api/local-outlook/jobs/'+'0'*36)[0])
        self.assertEqual([], self.service.adapter.calls)

    def test_unsupported_environment_is_explicit(self):
        self.service.adapter.supported = False
        self.assertFalse(self.call('/api/local-outlook')[1]['supported'])
        self.assertEqual(503, self.call('/api/local-outlook/discover', {'requestId':'x'})[0])


if __name__ == '__main__':
    unittest.main()
