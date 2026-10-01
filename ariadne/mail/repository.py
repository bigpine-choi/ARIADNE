"""Atomic mail snapshots and additive work links using the shared repository."""

from contextlib import contextmanager
from hashlib import sha256
import json
from uuid import uuid4

from ..models import ValidationError
from .models import validate_capture


TABLE_COLUMNS = {
    "mail_messages": {"id", "mailbox_id", "mailbox_username", "source_id", "current_revision"},
    "mail_revisions": {"mail_id", "revision", "evidence_id", "mailbox_username", "folder",
                       "captured_at", "content_hash", "payload", "search_text"},
}
SCHEMA_STATEMENTS = (
    """CREATE TABLE mail_messages (
        id TEXT PRIMARY KEY, mailbox_id TEXT NOT NULL, mailbox_username TEXT NOT NULL,
        source_id TEXT NOT NULL, current_revision INTEGER NOT NULL CHECK(current_revision >= 1),
        UNIQUE(mailbox_id, source_id)
    )""",
    """CREATE TABLE mail_revisions (
        mail_id TEXT NOT NULL REFERENCES mail_messages(id),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence(id),
        mailbox_username TEXT NOT NULL, folder TEXT NOT NULL CHECK(folder IN ('inbox','sentitems')),
        captured_at TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL,
        search_text TEXT NOT NULL, PRIMARY KEY(mail_id, revision)
    )""",
    "CREATE INDEX idx_mail_captured ON mail_revisions(captured_at, mail_id)",
)


class MailRepository:
    def __init__(self, repository):
        self._repo = repository

    @property
    def _db(self):
        return self._repo._connection

    @contextmanager
    def _reading(self):
        # A snapshot across count/details/revisions also remains coherent when
        # another ARIADNE process imports mail into the same database.
        with self._repo._lock:
            self._db.execute("BEGIN")
            try:
                yield
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def import_messages(self, capture, actor_id):
        from ..repository import _actor, _json
        _actor(actor_id, user=True)
        capture = validate_capture(capture)
        result = {"created": 0, "unchanged": 0, "revised": 0, "items": []}
        with self._repo._transaction():
            for message in capture["messages"]:
                digest = sha256(_json({"folder": capture["folder"], "message": message}).encode("utf-8")).hexdigest()
                existing = self._db.execute(
                    """SELECT m.id, m.current_revision, r.content_hash, r.evidence_id
                       FROM mail_messages m JOIN mail_revisions r
                       ON r.mail_id=m.id AND r.revision=m.current_revision
                       WHERE m.mailbox_id=? AND m.source_id=?""",
                    (capture["mailbox"]["id"], message["id"]),
                ).fetchone()
                if existing and existing["content_hash"] == digest:
                    result["unchanged"] += 1
                    result["items"].append({"mailId": existing["id"], "revision": existing["current_revision"],
                                            "evidenceId": existing["evidence_id"], "outcome": "unchanged"})
                    continue
                outcome = "revised" if existing else "created"
                mail_id = existing["id"] if existing else str(uuid4())
                revision = existing["current_revision"] + 1 if existing else 1
                evidence_id = str(uuid4())
                mailbox = capture["mailbox"]
                if not existing:
                    self._db.execute("INSERT INTO mail_messages VALUES (?,?,?,?,?)",
                                     (mail_id, mailbox["id"], mailbox["username"], message["id"], revision))
                evidence = {
                    "id": evidence_id, "provider": "outlook",
                    "sourceId": mailbox["id"] + ":" + message["id"], "capturedAt": capture["fetchedAt"],
                    "evidence": message["subject"].strip() or "제목 없는 Outlook 메일",
                    "locator": f"ariadne://mail/{mail_id}/revisions/{revision}", "sha256": digest,
                    "threadId": message["conversationId"] or None,
                    "finality": "draft" if message["isDraft"] else "unknown", "role": "context",
                }
                self._repo._insert_evidence(evidence, actor_id)
                searchable = "\n".join([message["subject"], message["fromName"], message["fromAddress"],
                                         message["bodyText"], mailbox["username"],
                                         *(entry["name"] for entry in message["attachments"])]).casefold()
                self._db.execute("INSERT INTO mail_revisions VALUES (?,?,?,?,?,?,?,?,?)",
                                 (mail_id, revision, evidence_id, mailbox["username"], capture["folder"],
                                  capture["fetchedAt"], digest, _json(message), searchable))
                if existing:
                    self._db.execute("UPDATE mail_messages SET current_revision=?,mailbox_username=? WHERE id=?",
                                     (revision, mailbox["username"], mail_id))
                summary = {"mailId": mail_id, "revision": revision, "evidenceId": evidence_id, "outcome": outcome}
                self._repo._audit("mail", mail_id, "mail_" + outcome, actor_id, "Selected Outlook mail snapshot saved",
                                  [evidence_id], None if not existing else {"revision": existing["current_revision"],
                                                                          "evidenceId": existing["evidence_id"]},
                                  {**summary, "contentHash": digest})
                result[outcome] += 1
                result["items"].append(summary)
        return result

    def search(self, query="", limit=50, offset=0):
        from .models import _text
        _text(query, "query", 200)
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValidationError("Search requires limit 1..50 and offset 0..1000000")
        escaped = query.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        relation = """FROM mail_messages m JOIN mail_revisions r
                      ON r.mail_id=m.id AND r.revision=m.current_revision
                      WHERE r.search_text LIKE ? ESCAPE '\\'"""
        with self._reading():
            total = self._db.execute("SELECT COUNT(*) " + relation, ("%" + escaped + "%",)).fetchone()[0]
            rows = self._db.execute("SELECT m.id,r.* " + relation + " ORDER BY r.captured_at DESC,m.id LIMIT ? OFFSET ?",
                                    ("%" + escaped + "%", limit, offset)).fetchall()
            items = []
            for row in rows:
                message = json.loads(row["payload"])
                items.append({"id": row["id"], "subject": message["subject"], "mailboxUsername": row["mailbox_username"],
                              "revision": row["revision"], "evidenceId": row["evidence_id"],
                              "receivedAt": message["receivedAt"], "sentAt": message["sentAt"],
                              "bodyTruncated": message["bodyTruncated"], "attachmentsTruncated": message["attachmentsTruncated"],
                              "attachmentCount": len(message["attachments"]), "folder": row["folder"],
                              "fromName": message["fromName"], "fromAddress": message["fromAddress"]})
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _revision(self, mail_id, revision=None):
        from ..repository import NotFoundError, _text, _version
        _text(mail_id, "mailId")
        if revision is not None:
            _version(revision)
        row = self._db.execute(
            """SELECT m.id,r.* FROM mail_messages m JOIN mail_revisions r ON r.mail_id=m.id
               WHERE m.id=? AND r.revision=COALESCE(?,m.current_revision)""", (mail_id, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError("Saved mail revision was not found")
        return row

    def get(self, mail_id, revision=None):
        with self._reading():
            row = self._revision(mail_id, revision)
            revisions = [{"revision": entry["revision"], "evidenceId": entry["evidence_id"], "capturedAt": entry["captured_at"]}
                         for entry in self._db.execute("SELECT revision,evidence_id,captured_at FROM mail_revisions WHERE mail_id=? ORDER BY revision DESC", (mail_id,))]
            linked = []
            for item in self._db.execute("SELECT id,payload,version FROM work_items ORDER BY id"):
                payload = json.loads(item["payload"])
                if row["evidence_id"] in payload["sources"]:
                    linked.append({"id": item["id"], "title": payload["title"], "version": item["version"]})
            return {"id": row["id"], "mailboxUsername": row["mailbox_username"], "revision": row["revision"],
                    "evidenceId": row["evidence_id"], "capturedAt": row["captured_at"], "contentHash": row["content_hash"],
                    "folder": row["folder"], "message": json.loads(row["payload"]), "revisions": revisions,
                    "linkedWorkItems": linked}

    def link(self, mail_id, revision, work_item_id, expected_version, actor_id, reason):
        from ..repository import ConflictError, _actor, _text, _version
        _actor(actor_id, user=True)
        _text(reason, "reason")
        _version(revision)
        _version(expected_version)
        with self._repo._transaction():
            mail = self._revision(mail_id, revision)
            row = self._repo._row(work_item_id)
            item = json.loads(row["payload"])
            evidence_id = mail["evidence_id"]
            # A repeated link is already satisfied and never edits the work item,
            # even if a retry carries its original pre-link version.
            if evidence_id in item["sources"]:
                return {"item": self._repo._view(row), "alreadyLinked": True, "evidenceId": evidence_id}
            if row["version"] != expected_version:
                raise ConflictError("Work item version has changed; reload before linking mail")
            updated = self._repo._update(work_item_id, {"sources": [*item["sources"], evidence_id]}, expected_version,
                                         actor_id, reason, [evidence_id])
            return {"item": updated, "alreadyLinked": False, "evidenceId": evidence_id}
