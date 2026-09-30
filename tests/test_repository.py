from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ariadne.models import ValidationError
from ariadne.repository import ConflictError, NotFoundError, Repository
from test_models import source, work_item


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ariadne.db"
        self.repo = Repository(self.path)
        self.repo.create_work_item(work_item(), "user", "Synthetic task reviewed")

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def test_failed_completion_rolls_back_item_and_audit(self):
        before = self.repo.get_work_item("work-1")
        history = self.repo.audit()
        with self.assertRaises(ValidationError):
            self.repo.update_work_item("work-1", {"status": "completed"}, 1, "user", "Review attempted")
        self.assertEqual(before, self.repo.get_work_item("work-1"))
        self.assertEqual(history, self.repo.audit())

    def test_audit_failure_rolls_back_actual_work_change(self):
        self.repo._connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.update_work_item("work-1", {"title": "Must roll back"}, 1, "user", "Atomic audit test")
        self.assertEqual(self.repo.get_work_item("work-1")["version"], 1)
        self.assertEqual(self.repo.get_work_item("work-1")["title"], "날인본 수령 검토")

    def test_completed_requires_each_condition_review_and_direct_evidence(self):
        self.repo.add_evidence(source("sent", "email_sent"))
        self.repo.add_evidence(source("signed", "completion"))
        patch = {"status": "completed", "sources": ["sent", "signed"],
                 "completionConditions": [{"id": "receive", "label": "원본 수령"}, {"id": "review", "label": "날인 확인"}],
                 "completionEvidence": [{"conditionId": "receive", "evidenceId": "signed",
                                         "reviewedBy": "user", "reviewedAt": "2026-06-19T18:00:00Z"}]}
        with self.assertRaises(ValidationError):
            self.repo.update_work_item("work-1", patch, 1, "user", "Missing review")
        patch["completionEvidence"].append({"conditionId": "review", "evidenceId": "sent",
                                            "reviewedBy": "user", "reviewedAt": "2026-06-19T18:01:00Z"})
        with self.assertRaises(ValidationError):
            self.repo.update_work_item("work-1", patch, 1, "user", "Sent mail is not completion")
        patch["completionEvidence"][1]["evidenceId"] = "signed"
        completed = self.repo.update_work_item("work-1", patch, 1, "user", "Both conditions reviewed", ["signed"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["dueRisk"], "none")
        self.assertEqual(self.repo.audit("work-1")[-1]["evidenceIds"], ["signed"])

    def test_completion_references_cannot_be_unattached_or_automated_review(self):
        self.repo.add_evidence(source("signed", "completion"))
        patch = {"completionConditions": [{"id": "review", "label": "검토"}],
                 "completionEvidence": [{"conditionId": "review", "evidenceId": "signed",
                                         "reviewedBy": "user", "reviewedAt": "2026-06-19T18:00:00Z"}]}
        with self.assertRaises(ValidationError):
            self.repo.update_work_item("work-1", patch, 1, "user", "Detached evidence")
        patch["sources"] = ["signed"]
        patch["completionEvidence"][0]["reviewedBy"] = "ai"
        with self.assertRaises(ValidationError):
            self.repo.update_work_item("work-1", patch, 1, "user", "Automated review")

    def test_due_risk_is_separate_and_unconfirmed_dates_do_not_become_overdue(self):
        self.repo.add_evidence(source())
        due = {**work_item()["due"], "kind": "date", "date": "2026-06-19", "confirmed": True, "evidenceIds": ["evidence-1"]}
        self.repo.update_work_item("work-1", {"due": due, "sources": ["evidence-1"]}, 1, "user", "Confirmed deadline")
        for when, risk in [("2026-06-18", "none"), ("2026-06-19", "due_today"), ("2026-06-20", "overdue")]:
            value = self.repo.get_work_item("work-1", when)
            self.assertEqual(value["dueRisk"], risk)
            self.assertEqual(value["status"], "waiting_external")
        due["confirmed"] = False
        self.repo.update_work_item("work-1", {"due": due}, 2, "user", "Date no longer confirmed")
        self.assertEqual(self.repo.get_work_item("work-1", "2026-06-20")["dueRisk"], "unknown")

    def test_evidence_id_and_source_identity_are_immutable_and_idempotent(self):
        self.repo.add_evidence(source())
        history = self.repo.audit()
        self.repo.add_evidence(source())
        self.assertEqual(history, self.repo.audit())
        with self.assertRaises(ConflictError):
            self.repo.add_evidence({**source(), "evidence": "Altered excerpt"})
        with self.assertRaises(ConflictError):
            self.repo.add_evidence({**source(), "id": "duplicate-source"})
        second_clause = {**source(), "id": "separate-anchor", "locator": "synthetic://fixture#clause-2"}
        self.repo.add_evidence(second_clause)
        self.assertEqual(len(self.repo.list_evidence()), 2)

    def test_draft_completion_and_unknown_schema_version_are_rejected(self):
        self.repo.add_evidence({**source("draft", "completion"), "finality": "draft"})
        patch = {"status": "completed", "sources": ["draft"],
                 "completionConditions": [{"id": "review", "label": "검토"}],
                 "completionEvidence": [{"conditionId": "review", "evidenceId": "draft", "reviewedBy": "user", "reviewedAt": "2026-06-19T18:00:00Z"}]}
        with self.assertRaises(ValidationError):
            self.repo.update_work_item("work-1", patch, 1, "user", "Draft is not final")
        future = Path(self.temp.name) / "future.db"
        with closing(sqlite3.connect(future)) as database:
            database.execute("PRAGMA user_version = 99")
        with self.assertRaises(ConflictError):
            Repository(future)

    def test_ai_proposals_do_not_mutate_and_only_explicit_user_resolution_applies(self):
        before = self.repo.get_work_item("work-1")
        proposal = self.repo.propose("work-1", {"ownerId": "reviewer", "status": "in_progress"}, 1)
        self.assertEqual(before, self.repo.get_work_item("work-1"))
        self.assertEqual(proposal["conflictFields"], ["ownerId", "status"])
        for actor in ("ai", "SYSTEM", " ai "):
            with self.subTest(actor=actor):
                with self.assertRaises(ValidationError):
                    self.repo.update_work_item("work-1", {"status": "completed"}, 1, actor, "Automated attempt")
                with self.assertRaises(ValidationError):
                    self.repo.resolve_proposal(proposal["id"], "accepted", 1, actor, "Automated acceptance")
        self.repo.resolve_proposal(proposal["id"], "accepted", 1, "user", "Suggestion reviewed")
        updated = self.repo.get_work_item("work-1")
        self.assertEqual(updated["ownerId"], "reviewer")
        self.assertEqual(updated["version"], 2)
        self.assertIn("ownerId", updated["confirmedFields"])
        self.assertEqual([e["eventType"] for e in self.repo.audit("work-1")][-2:], ["work_item_updated", "proposal_resolved"])

    def test_stale_proposal_cannot_overwrite_user_but_can_be_rejected(self):
        proposal = self.repo.propose("work-1", {"title": "AI title"}, 1)
        self.repo.update_work_item("work-1", {"title": "User title"}, 1, "user", "Human correction")
        with self.assertRaises(ConflictError):
            self.repo.resolve_proposal(proposal["id"], "accepted", 2, "user", "Stale acceptance")
        self.assertEqual(self.repo.list_proposals("work-1")[0]["status"], "pending")
        self.repo.resolve_proposal(proposal["id"], "rejected", 2, "user", "Newer title prevails")
        self.assertEqual(self.repo.get_work_item("work-1")["title"], "User title")

    def test_extraction_and_email_activity_do_not_complete_or_edit_task(self):
        self.repo.add_evidence(source("sent", "email_sent"))
        before = self.repo.get_work_item("work-1")
        extraction = self.repo.record_extraction("extract-1", ["sent"], {"status": "completed"}, "fixture-model")
        self.assertEqual(self.repo.record_extraction("extract-1", ["sent"], {"status": "completed"}, "fixture-model"), extraction)
        self.repo.record_activity("work-1", "email_sent", ["sent"], "user", "Request sent")
        self.assertEqual(before, self.repo.get_work_item("work-1"))

    def test_concurrent_stale_writers_have_exactly_one_winner(self):
        other = Repository(self.path)
        try:
            def update(repo, title):
                try:
                    repo.update_work_item("work-1", {"title": title}, 1, "user", "Concurrent edit")
                    return "updated"
                except ConflictError:
                    return "conflict"
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(update, self.repo, "Title A")
                b = pool.submit(update, other, "Title B")
                self.assertCountEqual([a.result(), b.result()], ["updated", "conflict"])
            self.assertEqual(self.repo.get_work_item("work-1")["version"], 2)
            self.assertEqual(len(self.repo.audit("work-1")), 2)
        finally:
            other.close()

    def test_boolean_versions_and_missing_sources_never_write(self):
        for method in [lambda: self.repo.update_work_item("work-1", {"title": "x"}, True, "user", "Bad version"),
                       lambda: self.repo.propose("work-1", {"title": "x"}, True),
                       lambda: self.repo.update_work_item("work-1", {"sources": ["missing"]}, 1, "user", "Missing source")]:
            with self.assertRaises((ValidationError, NotFoundError)):
                method()
        self.assertEqual(len(self.repo.audit("work-1")), 1)

    def test_backup_and_fund_alias_registry_survive_restart(self):
        self.repo.add_evidence(source())
        self.repo.register_fund("demo-1", "Demo Fund", ["데모 펀드"])
        self.repo.backup(Path(self.temp.name) / "backup.db")
        with Repository(Path(self.temp.name) / "backup.db") as backup:
            self.assertEqual(backup.get_work_item("work-1"), self.repo.get_work_item("work-1"))
            self.assertEqual(backup.list_evidence(), self.repo.list_evidence())
            self.assertEqual(backup.audit(), self.repo.audit())
            self.assertEqual(backup.resolve_fund("demo fund"), "demo-1")
            backup.register_fund("demo-2", "Other", ["데모 펀드"])
            with self.assertRaises(ValidationError):
                backup.resolve_fund("데모 펀드")
        with self.assertRaises(ConflictError):
            self.repo.backup(Path(self.temp.name) / "backup.db")


if __name__ == "__main__":
    unittest.main()
