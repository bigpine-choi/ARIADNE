from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from ariadne.models import ValidationError
from ariadne.repository import ConflictError, NotFoundError, Repository
from test_models import source, work_item


def mail_message(identity="immutable-1", **changes):
    return {
        "id": identity, "internetMessageId": "<message@example.test>", "subject": "분배 일정 확인",
        "fromName": "담당자", "fromAddress": "sender@example.test",
        "toRecipients": [{"name": "사용자", "address": "user@example.test"}],
        "receivedAt": "2026-10-01T00:00:00Z", "sentAt": "2026-09-30T23:59:00Z",
        "bodyText": "확정된 기한이 아닙니다.", "bodyTruncated": False,
        "recipientsTruncated": False, "hasAttachments": True,
        "conversationId": "thread-1", "lastModifiedAt": "2026-10-01T00:00:00Z",
        "changeKey": "change-1", "isDraft": False,
        "attachments": [{"id": "attachment-1", "name": "자금집행.xlsx",
                         "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         "size": 4096, "isInline": False, "kind": "file",
                         "lastModifiedAt": "2026-10-01T00:00:00Z", "state": "not_downloaded"}],
        "attachmentsTruncated": False, **changes,
    }


def mail_capture(*messages, mailbox_id="a" * 64, **changes):
    return {"mailbox": {"id": mailbox_id, "username": "user@example.test"}, "folder": "inbox",
            "fetchedAt": "2026-10-01T00:01:00Z", "messages": list(messages or [mail_message()]), **changes}


class MailRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mail.sqlite3"
        self.repo = Repository(self.path)
        self.addCleanup(self.repo.close)

    def save(self, capture=None):
        return self.repo.mail.import_messages(capture or mail_capture(), "user")

    def test_save_is_detached_durable_and_does_not_create_work(self):
        capture = mail_capture()
        saved = self.save(capture)
        self.assertEqual((1, 0, 0), (saved["created"], saved["unchanged"], saved["revised"]))
        row = saved["items"][0]
        capture["messages"][0]["subject"] = "Mutated after return"
        self.assertEqual([], self.repo.list_work_items())
        with Repository(self.path) as reopened:
            detail = reopened.mail.get(row["mailId"])
            self.assertEqual("분배 일정 확인", detail["message"]["subject"])
            self.assertEqual(row["evidenceId"], detail["evidenceId"])
            self.assertEqual("not_downloaded", detail["message"]["attachments"][0]["state"])
            self.assertEqual([], detail["linkedWorkItems"])
            self.assertEqual(1, reopened.mail.search()["total"])

    def test_duplicate_capture_time_changes_no_revision_audit_or_work_version(self):
        self.repo.create_work_item(work_item(), "user", "Reviewed")
        first = self.save()["items"][0]
        audit = self.repo.audit()
        duplicate = self.save(mail_capture(fetchedAt="2026-10-02T00:00:00Z"))
        self.assertEqual({"created": 0, "revised": 0, "unchanged": 1}, {key: duplicate[key] for key in ("created", "revised", "unchanged")})
        self.assertEqual(first["evidenceId"], duplicate["items"][0]["evidenceId"])
        self.assertEqual(audit, self.repo.audit())
        self.assertEqual(1, self.repo.get_work_item("work-1")["version"])
        self.assertEqual("2026-10-01T00:01:00Z", self.repo.mail.get(first["mailId"])["capturedAt"])

    def test_revisions_preserve_a_b_a_snapshots_and_immutable_evidence(self):
        first = self.save()["items"][0]
        second = self.save(mail_capture(mail_message(bodyText="Changed", changeKey="change-2")))["items"][0]
        third = self.save()["items"][0]
        self.assertEqual([1, 2, 3], [entry["revision"] for entry in (first, second, third)])
        self.assertEqual(3, len({entry["evidenceId"] for entry in (first, second, third)}))
        old = self.repo.mail.get(first["mailId"], 1)
        latest = self.repo.mail.get(first["mailId"])
        self.assertEqual(old["contentHash"], latest["contentHash"])
        self.assertNotEqual(old["evidenceId"], latest["evidenceId"])
        self.assertEqual("Changed", self.repo.mail.get(first["mailId"], 2)["message"]["bodyText"])
        self.assertEqual([3, 2, 1], [row["revision"] for row in latest["revisions"]])
        evidence = self.repo.get_evidence(first["evidenceId"])
        with self.assertRaises(ConflictError):
            self.repo.add_evidence({**evidence, "evidence": "Changed snapshot"})

    def test_change_key_and_partial_flags_detect_hidden_changes(self):
        first = self.save(mail_capture(mail_message(bodyTruncated=True)))["items"][0]
        changed = self.save(mail_capture(mail_message(bodyTruncated=True, changeKey="unseen-body-change")))
        self.assertEqual(1, changed["revised"])
        changed = self.save(mail_capture(mail_message(bodyTruncated=False, changeKey="unseen-body-change")))
        self.assertEqual(1, changed["revised"])
        self.assertEqual(3, self.repo.mail.get(first["mailId"])["revision"])

    def test_attachment_order_does_not_create_a_spurious_revision(self):
        first_attachment = mail_message()["attachments"][0]
        second_attachment = {**first_attachment, "id": "attachment-2", "name": "두 번째.xlsx"}
        message = mail_message(attachments=[second_attachment, first_attachment])
        saved = self.save(mail_capture(message))["items"][0]
        before = self.repo.audit()
        message["attachments"].reverse()
        result = self.save(mail_capture(message))
        self.assertEqual(1, result["unchanged"])
        self.assertEqual(before, self.repo.audit())
        self.assertEqual(["attachment-1", "attachment-2"],
                         [entry["id"] for entry in self.repo.mail.get(saved["mailId"])["message"]["attachments"]])

    def test_same_immutable_id_is_separate_per_mailbox_and_move_is_revision(self):
        first = self.save()["items"][0]
        other = self.save(mail_capture(mailbox_id="b" * 64))["items"][0]
        self.assertNotEqual(first["mailId"], other["mailId"])
        moved = self.save(mail_capture(folder="sentitems"))["items"][0]
        self.assertEqual(first["mailId"], moved["mailId"])
        self.assertEqual("revised", moved["outcome"])

    def test_unknown_secret_and_provider_payload_fields_rejected_without_writes(self):
        cases = []
        cases.append({**mail_capture(), "access_token": "secret"})
        cases.append(mail_capture(mail_message(contentBytes="raw attachment")))
        attachment = deepcopy(mail_message()["attachments"])
        attachment[0]["contentBytes"] = "raw attachment"
        cases.append(mail_capture(mail_message(attachments=attachment)))
        cases.append({**mail_capture(), "mailbox": {**mail_capture()["mailbox"], "cache": "secret"}})
        for capture in cases:
            with self.subTest(capture=sorted(capture)):
                with self.assertRaises(ValidationError):
                    self.save(capture)
        self.assertEqual(0, self.repo.mail.search()["total"])
        self.assertEqual([], self.repo.audit())
        self.assertEqual([], self.repo.list_evidence())

    def test_bounds_types_and_offset_dates_validate_before_batch_writes(self):
        invalid = [mail_message(bodyText="x" * 100001), mail_message(isDraft=1),
                   mail_message(receivedAt="2026-10-01"), mail_message(sentAt="2026-02-30T00:00:00Z"),
                   mail_message(changeKey=""), mail_message(conversationId=None),
                   mail_message(id="x" * 2049), mail_message(bodyText="\ud800"),
                   mail_message(attachments=[{**mail_message()["attachments"][0], "size": True}]),
                   mail_message(attachments=[{**mail_message()["attachments"][0], "state": "downloaded"}])]
        for message in invalid:
            with self.subTest(field=[key for key in message if message[key] != mail_message().get(key)]):
                with self.assertRaises(ValidationError):
                    self.save(mail_capture(mail_message("valid-first"), message))
        for capture in (mail_capture(messages=[]), mail_capture(*[mail_message(str(n)) for n in range(11)]),
                        mail_capture(mail_message(), mail_message()), mail_capture(mailbox_id="not-a-mailbox")):
            with self.assertRaises(ValidationError):
                self.save(capture)
        self.assertEqual([], self.repo.audit())
        self.assertEqual(0, self.repo.mail.search()["total"])
        self.save(mail_capture(mail_message(bodyText="x" * 100000)))

    def test_failure_in_later_batch_item_rolls_back_mail_evidence_and_audit(self):
        self.repo._connection.execute("""CREATE TRIGGER reject_mail BEFORE INSERT ON mail_revisions
            WHEN NEW.payload LIKE '%FAIL_SECOND%' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save(mail_capture(mail_message("first"), mail_message("second", subject="FAIL_SECOND")))
        self.assertEqual(0, self.repo.mail.search()["total"])
        self.assertEqual([], self.repo.list_evidence())
        self.assertEqual([], self.repo.audit())

    def test_concurrent_connections_import_exactly_one_snapshot(self):
        gate = Barrier(2)
        def save_once():
            with Repository(self.path) as repo:
                gate.wait(timeout=5)
                return repo.mail.import_messages(mail_capture(), "user")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save_once(), range(2)))
        self.assertEqual(1, sum(result["created"] for result in results))
        self.assertEqual(1, sum(result["unchanged"] for result in results))
        self.assertEqual(1, len(self.repo.list_evidence()))
        self.assertEqual(2, len(self.repo.audit()))

    def test_search_is_current_only_unicode_casefold_and_literal_wildcards(self):
        first = self.save(mail_capture(mail_message(subject="Straße 100%_literal"), mail_message("second", subject="다른 메일")))["items"][0]
        self.assertEqual(1, self.repo.mail.search("STRASSE")["total"])
        self.assertEqual(1, self.repo.mail.search("%_literal")["total"])
        self.assertEqual(0, self.repo.mail.search("%not-a-wildcard")["total"])
        self.assertEqual(2, self.repo.mail.search("자금집행.xlsx")["total"])
        self.assertEqual(2, self.repo.mail.search(limit=1, offset=1)["total"])
        self.assertEqual(1, len(self.repo.mail.search(limit=1, offset=1)["items"]))
        self.save(mail_capture(mail_message(subject="새 제목", changeKey="change-2")))
        self.assertEqual(0, self.repo.mail.search("STRASSE")["total"])
        self.assertIn("Straße", self.repo.mail.get(first["mailId"], 1)["message"]["subject"])
        for kwargs in ({"limit": True}, {"limit": 0}, {"offset": -1}, {"query": "x" * 201}):
            with self.assertRaises(ValidationError):
                self.repo.mail.search(**kwargs)

    def test_link_adds_only_source_idempotently_and_preserves_confirmed_fields(self):
        self.repo.add_evidence(source())
        item = work_item()
        item["sources"] = ["evidence-1"]
        before = self.repo.create_work_item(item, "user", "User reviewed")
        saved = self.save()["items"][0]
        result = self.repo.mail.link(saved["mailId"], 1, "work-1", 1, "user", "Link context")
        after = result["item"]
        self.assertFalse(result["alreadyLinked"])
        self.assertEqual(["evidence-1", saved["evidenceId"]], after["sources"])
        self.assertEqual(2, after["version"])
        for key in before.keys() - {"version", "sources"}:
            self.assertEqual(before[key], after[key], key)
        history = self.repo.audit()
        repeated = self.repo.mail.link(saved["mailId"], 1, "work-1", 1, "user", "Retry after response lost")
        self.assertTrue(repeated["alreadyLinked"])
        self.assertEqual(after, repeated["item"])
        self.assertEqual(history, self.repo.audit())

    def test_stale_new_link_and_automated_link_cannot_overwrite_user_edit(self):
        self.repo.create_work_item(work_item(), "user", "User reviewed")
        saved = self.save()["items"][0]
        updated = self.repo.update_work_item("work-1", {"title": "사용자가 수정한 업무"}, 1, "user", "Confirmed")
        before = self.repo.audit()
        with self.assertRaises(ConflictError):
            self.repo.mail.link(saved["mailId"], 1, "work-1", 1, "user", "Stale link")
        with self.assertRaises(ValidationError):
            self.repo.mail.link(saved["mailId"], 1, "work-1", 2, "ai", "Automated link")
        self.assertEqual(updated, self.repo.get_work_item("work-1"))
        self.assertEqual(before, self.repo.audit())

    def test_new_revision_does_not_retarget_an_existing_work_link(self):
        self.repo.create_work_item(work_item(), "user", "Reviewed")
        first = self.save()["items"][0]
        self.repo.mail.link(first["mailId"], 1, "work-1", 1, "user", "Context")
        before = self.repo.get_work_item("work-1")
        self.save(mail_capture(mail_message(subject="Changed", changeKey="changed")))
        self.assertEqual(before, self.repo.get_work_item("work-1"))
        self.assertEqual([], self.repo.mail.get(first["mailId"])["linkedWorkItems"])
        self.assertEqual("work-1", self.repo.mail.get(first["mailId"], 1)["linkedWorkItems"][0]["id"])

    def test_context_and_draft_mail_cannot_establish_completion(self):
        self.repo.create_work_item(work_item(), "user", "Reviewed")
        saved = self.save(mail_capture(mail_message(isDraft=True)))["items"][0]
        evidence = self.repo.get_evidence(saved["evidenceId"])
        self.assertEqual(("context", "draft"), (evidence["role"], evidence["finality"]))
        with self.assertRaises(ValidationError):
            self.repo.update_work_item("work-1", {"status": "completed", "sources": [saved["evidenceId"]],
                "completionConditions": [{"id": "done", "label": "검토 완료"}],
                "completionEvidence": [{"conditionId": "done", "evidenceId": saved["evidenceId"],
                                        "reviewedBy": "user", "reviewedAt": "2026-10-01T00:00:00Z"}]},
                1, "user", "Mail is not completion proof")

    def test_link_audit_failure_rolls_back_sources_and_version(self):
        before = self.repo.create_work_item(work_item(), "user", "Reviewed")
        saved = self.save()["items"][0]
        history = self.repo.audit()
        self.repo._connection.execute("CREATE TRIGGER fail_link BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.mail.link(saved["mailId"], 1, "work-1", 1, "user", "Link")
        self.assertEqual(before, self.repo.get_work_item("work-1"))
        self.assertEqual(history, self.repo.audit())

    def test_backup_and_missing_revision(self):
        saved = self.save()["items"][0]
        backup = Path(self.temp.name) / "backup.sqlite3"
        self.repo.backup(backup)
        with Repository(backup) as restored:
            self.assertEqual(self.repo.mail.get(saved["mailId"]), restored.mail.get(saved["mailId"]))
        for mail_id, revision in (("unknown", None), (saved["mailId"], 99)):
            with self.assertRaises(NotFoundError):
                self.repo.mail.get(mail_id, revision)


class MailMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "legacy.sqlite3"

    def legacy(self):
        with Repository(self.path) as repo:
            repo.add_evidence(source())
            item = work_item()
            item["sources"] = ["evidence-1"]
            repo.create_work_item(item, "user", "Legacy confirmed task")
            repo.update_work_item("work-1", {"title": "기존 사용자 수정"}, 1, "user", "Preserve user edit")
            repo.record_activity("work-1", "legacy_review", ["evidence-1"], "user", "Keep audit")
            repo.record_extraction("legacy-extraction", ["evidence-1"], {"title": "AI suggestion"}, "synthetic")
            repo.propose("work-1", {"title": "Review candidate"}, 2, reason="Unaccepted proposal")
            repo.register_fund("fund-1", "펀드", ["별칭"])
            repo._connection.execute("DROP TABLE mail_revisions")
            repo._connection.execute("DROP TABLE mail_messages")
            repo._connection.execute("PRAGMA user_version=1")
        return self.snapshot()

    def snapshot(self):
        connection = sqlite3.connect(self.path)
        try:
            return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                    for table in Repository.LEGACY_TABLE_COLUMNS}
        finally:
            connection.close()

    def test_schema1_upgrade_preserves_every_legacy_cell_without_reencoding(self):
        original = self.legacy()
        with Repository(self.path) as repo:
            self.assertEqual(2, repo._connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(0, repo.mail.search()["total"])
            self.assertEqual(original, self.snapshot())
            self.assertEqual(2, repo.get_work_item("work-1")["version"])
            self.assertEqual("pending", repo.list_proposals("work-1")[0]["status"])
            repo.mail.import_messages(mail_capture(), "user")
        with Repository(self.path) as repo:
            self.assertEqual(1, repo.mail.search()["total"])

    def test_failed_ddl_upgrade_rolls_back_tables_and_version(self):
        original = self.legacy()
        from ariadne.mail.repository import SCHEMA_STATEMENTS
        with patch("ariadne.repository.MAIL_SCHEMA", (*SCHEMA_STATEMENTS, "INVALID SQL")):
            with self.assertRaises(sqlite3.OperationalError):
                Repository(self.path)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual([], connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'mail_%'").fetchall())
        finally:
            connection.close()
        self.assertEqual(original, self.snapshot())
        with Repository(self.path) as repo:
            self.assertEqual(0, repo.mail.search()["total"])

    def test_concurrent_migration_keeps_initial_identity_reads_in_one_snapshot(self):
        original = self.legacy()
        version_read, migration_done = Event(), Event()
        real_connect = sqlite3.connect

        class PausingConnection(sqlite3.Connection):
            paused = False

            def execute(self, sql, *args):
                cursor = super().execute(sql, *args)
                if sql == "PRAGMA user_version" and not self.paused:
                    self.paused = True
                    version_read.set()
                    if not migration_done.wait(timeout=5):
                        raise TimeoutError("Concurrent migration did not finish")
                return cursor

        def open_paused():
            with patch("ariadne.repository.sqlite3.connect",
                       side_effect=lambda *args, **kwargs: real_connect(*args, factory=PausingConnection, **kwargs)):
                return Repository(self.path)

        # Only the first constructor uses the paused connection. Migrate using
        # the historical DDL directly while its version read is suspended.
        from ariadne.mail.repository import SCHEMA_STATEMENTS
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(open_paused)
            self.assertTrue(version_read.wait(timeout=5))
            connection = real_connect(self.path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=2")
                connection.commit()
            finally:
                connection.close()
                migration_done.set()
            repo = pending.result(timeout=5)
        try:
            self.assertEqual(2, repo._connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(original, self.snapshot())
        finally:
            repo.close()

    def test_unknown_versions_foreign_identity_and_objects_refused_without_byte_changes(self):
        cases = [(99, Repository.APPLICATION_ID, False), (1, 0, False),
                 (2, Repository.APPLICATION_ID, True), (1, Repository.APPLICATION_ID, True)]
        for version, application_id, foreign in cases:
            with self.subTest(version=version, application_id=application_id, foreign=foreign):
                self.path.unlink(missing_ok=True)
                self.legacy()
                connection = sqlite3.connect(self.path)
                try:
                    connection.execute("PRAGMA journal_mode=DELETE")
                    connection.execute(f"PRAGMA user_version={version}")
                    connection.execute(f"PRAGMA application_id={application_id}")
                    if foreign:
                        connection.execute("CREATE TABLE unrelated (value TEXT)")
                    connection.commit()
                finally:
                    connection.close()
                before = sha256(self.path.read_bytes()).digest()
                with self.assertRaises(ConflictError):
                    Repository(self.path)
                self.assertEqual(before, sha256(self.path.read_bytes()).digest())


if __name__ == "__main__":
    unittest.main()
