"""Independent week-one user workflows, using synthetic evidence only."""

from copy import deepcopy
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
import json
import sqlite3
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ariadne.models import ValidationError
from ariadne.repository import ConflictError, Repository
from ariadne.server import create_server


def evidence(identifier="proof-1", role="completion"):
    return {
        "id": identifier,
        "provider": "synthetic",
        "sourceId": "synthetic:" + identifier,
        "capturedAt": "2026-10-01T09:00:00+09:00",
        "evidence": "가상 업무의 처리 완료를 확인했습니다.",
        "locator": "가상 문서 / 처리확인 문단",
        "sha256": None,
        "threadId": None,
        "finality": "final",
        "role": role,
    }


def work_item(identifier="task-1"):
    return {
        "id": identifier,
        "title": "가상 날인본 수령 및 검토",
        "fundId": None,
        "ownerId": None,
        "status": "waiting_external",
        "reviewRequired": True,
        "confidence": "medium",
        "due": {
            "kind": "unknown",
            "rawText": "회신 일정 확인 필요",
            "date": None,
            "timezone": "Asia/Seoul",
            "anchorTaskId": None,
            "confirmed": False,
            "evidenceIds": [],
        },
        "sources": [],
        "completionConditions": [{"id": "checked", "label": "날인 및 첨부 검토 완료"}],
        "completionEvidence": [],
        "waitingReason": "상대방의 날인본 회신을 기다립니다.",
    }


class WeekOneAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "workflow.sqlite3"
        self.repository = Repository(self.database)
        self.addCleanup(self.repository.close)

    def create(self, item=None):
        return self.repository.create_work_item(
            item or work_item(), actor_id="tester", reason="가상 사례 등록"
        )

    def test_email_sent_and_attachment_received_are_not_completion(self):
        for role in ("email_sent", "attachment_received"):
            with self.subTest(role=role):
                proof = evidence("proof-" + role, role)
                self.repository.add_evidence(proof)
                item = work_item("task-" + role)
                item["sources"] = [proof["id"]]
                saved = self.create(item)
                before_audit = len(self.repository.audit(item["id"]))
                patch = {
                    "status": "completed",
                    "completionEvidence": [{
                        "conditionId": "checked",
                        "evidenceId": proof["id"],
                        "reviewedBy": "tester",
                        "reviewedAt": "2026-10-01T09:30:00+09:00",
                    }],
                }
                with self.assertRaises(ValidationError):
                    self.repository.update_work_item(
                        item["id"], patch, saved["version"], "tester", "완료 요청"
                    )
                self.assertEqual("waiting_external", self.repository.get_work_item(item["id"])["status"])
                self.assertEqual(before_audit, len(self.repository.audit(item["id"])))

    def test_reviewed_completion_and_evidence_survive_restart(self):
        proof = evidence()
        self.repository.add_evidence(proof)
        item = work_item()
        item["sources"] = [proof["id"]]
        initial = self.create(item)
        completed = self.repository.update_work_item(
            item["id"], {
                "status": "completed",
                "completionEvidence": [{
                    "conditionId": "checked",
                    "evidenceId": proof["id"],
                    "reviewedBy": "tester",
                    "reviewedAt": "2026-10-01T09:30:00+09:00",
                }],
            }, initial["version"], "tester", "날인·첨부 검토 완료", [proof["id"]]
        )
        restarted = Repository(self.database)
        self.addCleanup(restarted.close)
        persisted = restarted.get_work_item(item["id"])
        self.assertEqual("completed", persisted["status"])
        self.assertEqual(completed["version"], persisted["version"])
        self.assertEqual(proof, restarted.get_evidence(proof["id"]))
        self.assertIn("status", persisted["confirmedFields"])
        self.assertGreaterEqual(len(restarted.audit(item["id"])), 2)

    def test_due_risk_does_not_replace_waiting_state(self):
        proof = evidence("deadline", "request")
        self.repository.add_evidence(proof)
        item = work_item()
        item["sources"] = [proof["id"]]
        item["due"] = {
            "kind": "date",
            "rawText": "2026년 9월 30일까지 회신",
            "date": "2026-09-30",
            "timezone": "Asia/Seoul",
            "anchorTaskId": None,
            "confirmed": True,
            "evidenceIds": [proof["id"]],
        }
        self.create(item)
        today = self.repository.get_work_item(item["id"], as_of="2026-09-30")
        tomorrow = self.repository.get_work_item(item["id"], as_of="2026-10-01")
        self.assertEqual("due_today", today["dueRisk"])
        self.assertEqual("overdue", tomorrow["dueRisk"])
        self.assertEqual("waiting_external", tomorrow["status"])
        self.assertEqual(item["waitingReason"], tomorrow["waitingReason"])

    def test_deadline_requires_attached_source_and_real_calendar_date(self):
        proof = evidence("deadline", "request")
        self.repository.add_evidence(proof)
        for date, sources in (("2026-02-29", [proof["id"]]), ("2026-10-01", [])):
            with self.subTest(date=date, sources=sources):
                item = work_item()
                item["sources"] = sources
                item["due"].update(
                    kind="date", date=date, confirmed=True,
                    evidenceIds=[proof["id"]], rawText="원문 기한"
                )
                with self.assertRaises(ValidationError):
                    self.create(item)
        self.assertEqual([], self.repository.list_work_items())

    def test_ai_proposal_does_not_change_user_confirmed_values(self):
        original = self.create()
        proposal = self.repository.propose(
            original["id"], {"status": "in_progress"}, original["version"],
            actor_id="ai", reason="가상 추출 제안"
        )
        stored = self.repository.get_work_item(original["id"])
        self.assertEqual("waiting_external", stored["status"])
        self.assertEqual(original["version"], stored["version"])
        self.assertEqual(1, len(self.repository.list_proposals(original["id"])))
        self.repository.resolve_proposal(
            proposal["id"], "rejected", original["version"], "tester", "회신 대기 유지"
        )
        self.assertEqual("waiting_external", self.repository.get_work_item(original["id"])["status"])

    def test_stale_update_cannot_erase_newer_user_change(self):
        initial = self.create()
        updated = self.repository.update_work_item(
            initial["id"], {"title": "사용자가 확인한 업무명"},
            initial["version"], "tester", "업무명 확정"
        )
        before_audit = len(self.repository.audit(initial["id"]))
        with self.assertRaises(ConflictError):
            self.repository.update_work_item(
                initial["id"], {"title": "오래된 창의 업무명"},
                initial["version"], "tester", "오래된 창에서 저장"
            )
        self.assertEqual("사용자가 확인한 업무명", self.repository.get_work_item(initial["id"])["title"])
        self.assertEqual(updated["version"], self.repository.get_work_item(initial["id"])["version"])
        self.assertEqual(before_audit, len(self.repository.audit(initial["id"])))

    def test_automated_actor_cannot_bypass_manual_proposal_review(self):
        saved = self.create()
        with self.assertRaises(ValidationError):
            self.repository.update_work_item(
                saved["id"], {"title": "AI가 덮어쓴 업무명"},
                saved["version"], " AI ", "자동 덮어쓰기 시도"
            )
        proposal = self.repository.propose(
            saved["id"], {"title": "AI가 제안한 업무명"}, saved["version"], reason="변경 제안"
        )
        with self.assertRaises(ValidationError):
            self.repository.resolve_proposal(
                proposal["id"], "accepted", saved["version"], "ai", "자기 제안 승인 시도"
            )
        self.assertEqual(saved["title"], self.repository.get_work_item(saved["id"])["title"])
        self.assertEqual("pending", self.repository.list_proposals(saved["id"])[0]["status"])

    def test_user_can_accept_conflict_explicitly_and_change_is_audited(self):
        saved = self.create()
        proposal = self.repository.propose(
            saved["id"], {"title": "사용자가 승인할 새 업무명"}, saved["version"], reason="변경 제안"
        )
        self.assertIn("title", proposal["conflictFields"])
        self.repository.resolve_proposal(
            proposal["id"], "accepted", saved["version"], "tester", "근거 검토 후 변경 승인"
        )
        updated = self.repository.get_work_item(saved["id"])
        self.assertEqual("사용자가 승인할 새 업무명", updated["title"])
        self.assertEqual(saved["version"] + 1, updated["version"])
        self.assertIn("title", updated["confirmedFields"])
        changes = [entry for entry in self.repository.audit(saved["id"]) if
                   entry["eventType"] == "work_item_updated"]
        self.assertEqual(1, len(changes))
        self.assertEqual(saved["title"], changes[0]["before"]["title"])
        self.assertEqual(updated["title"], changes[0]["after"]["title"])
        self.assertEqual("tester", changes[0]["actorId"])
        self.assertEqual("근거 검토 후 변경 승인", changes[0]["reason"])
        self.assertIn("timestamp", changes[0])

    def test_evidence_is_idempotent_and_cannot_be_rewritten(self):
        proof = evidence()
        self.repository.add_evidence(proof)
        self.repository.add_evidence(deepcopy(proof))
        self.assertEqual(1, len(self.repository.list_evidence()))
        rewritten = deepcopy(proof)
        rewritten["evidence"] = "기존 원문을 변경한 가상 문구"
        with self.assertRaises(ConflictError):
            self.repository.add_evidence(rewritten)
        self.assertEqual(proof, self.repository.get_evidence(proof["id"]))

    def test_backup_reopens_with_work_evidence_and_history(self):
        proof = evidence()
        self.repository.add_evidence(proof)
        item = work_item()
        item["sources"] = [proof["id"]]
        saved = self.create(item)
        backup = Path(self.temporary.name) / "backup.sqlite3"
        self.repository.backup(backup)
        restored = Repository(backup)
        self.addCleanup(restored.close)
        self.assertEqual(saved["title"], restored.get_work_item(saved["id"])["title"])
        self.assertEqual(saved["version"], restored.get_work_item(saved["id"])["version"])
        self.assertEqual(proof, restored.get_evidence(proof["id"]))
        self.assertEqual(self.repository.audit(), restored.audit())

    def test_rejected_database_does_not_change_original_storage(self):
        for schema_version in (0, 1, 99):
            with self.subTest(schema_version=schema_version):
                original_path = Path(self.temporary.name) / f"unrelated-{schema_version}.sqlite3"
                with closing(sqlite3.connect(original_path)) as connection:
                    connection.execute("CREATE TABLE original_data (value TEXT)")
                    connection.execute("INSERT INTO original_data VALUES ('original')")
                    connection.execute(f"PRAGMA user_version = {schema_version}")
                    connection.commit()
                with self.assertRaises(ConflictError):
                    Repository(original_path)
                with closing(sqlite3.connect(original_path)) as connection:
                    self.assertEqual("delete", connection.execute("PRAGMA journal_mode").fetchone()[0])
                    self.assertEqual(schema_version, connection.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual([("original",)], connection.execute("SELECT value FROM original_data").fetchall())


class LocalHttpAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary.name) / "http.sqlite3")
        self.server = create_server(self.repository, host="127.0.0.1", port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.repository.close()
        self.temporary.cleanup()

    def request(self, method, path, payload=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        data = payload if isinstance(payload, bytes) else (
            None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        request = Request(self.url + path, data=data, method=method, headers=request_headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_and_korean_create_read_roundtrip(self):
        status, body = self.request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertIsInstance(body, dict)
        status, body = self.request("POST", "/api/work-items", {
            "item": work_item(), "actorId": "tester", "reason": "한국어 입력 검증"
        })
        self.assertIn(status, (200, 201))
        self.assertEqual("가상 날인본 수령 및 검토", body["item"]["title"])
        status, body = self.request("GET", "/api/work-items/task-1")
        self.assertEqual(200, status)
        self.assertEqual("가상 날인본 수령 및 검토", body["item"]["title"])

    def test_other_origin_cannot_create_local_work(self):
        status, _ = self.request("POST", "/api/work-items", {
            "item": work_item(), "actorId": "tester", "reason": "차단되어야 하는 요청"
        }, headers={"Origin": "https://untrusted.example"})
        self.assertEqual(403, status)
        self.assertEqual([], self.repository.list_work_items())

    def test_rebound_host_cannot_read_evidence(self):
        self.repository.add_evidence(evidence())
        status, _ = self.request("GET", "/api/evidence", headers={"Host": "untrusted.example"})
        self.assertEqual(403, status)

    def test_json_versions_are_strict_and_failed_writes_do_not_mutate(self):
        saved = self.repository.create_work_item(work_item(), "tester", "가상 사례")
        for version in (str(saved["version"]), True, None):
            with self.subTest(version=version):
                status, _ = self.request("PATCH", "/api/work-items/task-1", {
                    "patch": {"title": "변경하면 안 되는 업무명"},
                    "actorId": "tester", "reason": "잘못된 버전", "expectedVersion": version,
                })
                self.assertIn(status, (400, 422))
                self.assertEqual(saved["title"], self.repository.get_work_item(saved["id"])["title"])
                self.assertEqual(saved["version"], self.repository.get_work_item(saved["id"])["version"])

    def test_non_json_browser_form_cannot_mutate(self):
        status, _ = self.request("POST", "/api/work-items", {
            "item": work_item(), "actorId": "tester", "reason": "폼 우회 요청"
        }, headers={"Content-Type": "text/plain"})
        self.assertIn(status, (400, 415))
        self.assertEqual([], self.repository.list_work_items())

    def test_unbounded_content_length_is_rejected_without_reading_body(self):
        for length in ("1048577", "9" * 5000):
            with self.subTest(length=length[:20]):
                status, _ = self.request(
                    "POST", "/api/work-items", b"{}", headers={"Content-Length": length}
                )
                self.assertEqual(400, status)
                self.assertEqual([], self.repository.list_work_items())

    def test_ambiguous_duplicate_json_fields_are_rejected(self):
        serialized_item = json.dumps(work_item(), ensure_ascii=False)
        body = ('{"item":' + serialized_item +
                ',"actorId":"tester","reason":"first","reason":"second"}').encode("utf-8")
        status, _ = self.request("POST", "/api/work-items", body)
        self.assertEqual(400, status)
        self.assertEqual([], self.repository.list_work_items())

    def test_invalid_proposal_decision_does_not_change_pending_work(self):
        saved = self.repository.create_work_item(work_item(), "tester", "가상 사례")
        proposal = self.repository.propose(
            saved["id"], {"status": "in_progress"}, saved["version"], reason="가상 제안"
        )
        for decision in ([], {}):
            with self.subTest(decision=decision):
                status, _ = self.request("POST", f"/api/proposals/{proposal['id']}/resolve", {
                    "decision": decision, "expectedVersion": saved["version"],
                    "actorId": "tester", "reason": "잘못된 선택",
                })
                self.assertEqual(400, status)
                self.assertEqual("pending", self.repository.list_proposals(saved["id"])[0]["status"])
                self.assertEqual(saved["status"], self.repository.get_work_item(saved["id"])["status"])


if __name__ == "__main__":
    unittest.main()

