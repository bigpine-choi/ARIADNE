"""Atomic local SQLite storage, evidence checks, review proposals, and audit history.

Actor identifiers are audit labels in this local prototype, not authentication.
Reserved automated actors cannot use direct user mutation methods.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from .models import MUTABLE_FIELDS, ValidationError, validate_date, validate_evidence, validate_work_item
from .funds import FundRegistry


class ConflictError(ValueError):
    """An identity or optimistic version conflicts with persisted state."""


class NotFoundError(LookupError):
    """The requested local entity does not exist."""


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ValidationError("Value must be finite JSON data") from None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, field, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValidationError(f"{field} must be a {'string' if empty else 'nonempty string'}")


def _version(value):
    if type(value) is not int or value < 1:
        raise ValidationError("Version must be a positive integer")


def _actor(actor_id, *, user=False):
    _text(actor_id, "actorId")
    if user and actor_id.strip().casefold() in {"ai", "system"}:
        raise ValidationError("Automated actors must submit a proposal for user review")


def _patch(patch):
    if not isinstance(patch, dict) or not patch or set(patch) - MUTABLE_FIELDS:
        raise ValidationError("Patch must contain only supported mutable fields")
    return deepcopy(patch)


class Repository:
    APPLICATION_ID = 0x41524941
    TABLE_COLUMNS = {
        "work_items": {"id", "payload", "version", "confirmed_fields"},
        "evidence": {"id", "provider", "source_id", "fingerprint", "locator", "payload"},
        "extractions": {"id", "payload"}, "funds": {"id", "payload"},
        "proposals": {"id", "work_item_id", "payload"},
        "activities": {"id", "work_item_id", "payload"},
        "audit_log": {"id", "work_item_id", "payload"},
    }

    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        schema_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        application_id = self._connection.execute("PRAGMA application_id").fetchone()[0]
        tables = {row[0] for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if schema_version not in {0, 1}:
            self._connection.close()
            raise ConflictError("Database schema version is unsupported")
        if schema_version == 0 and (tables or application_id != 0):
            self._connection.close()
            raise ConflictError("Unversioned legacy database requires an explicit migration")
        if schema_version == 1:
            if application_id != self.APPLICATION_ID or tables != set(self.TABLE_COLUMNS):
                self._connection.close()
                raise ConflictError("Database identity or structure is not recognized")
            for table, expected in self.TABLE_COLUMNS.items():
                columns = {row[1] for row in self._connection.execute(f'PRAGMA table_info("{table}")')}
                if columns != expected:
                    self._connection.close()
                    raise ConflictError("Database structure requires an explicit migration")
        # Persistent pragmas are applied only after accepting the database identity.
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version >= 1), confirmed_fields TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, source_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, locator TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(provider, source_id, fingerprint, locator)
            );
            CREATE TABLE IF NOT EXISTS extractions (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS funds (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_items(id), payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activities (
                id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_items(id), payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, work_item_id TEXT, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_work ON audit_log(work_item_id, id);
            CREATE INDEX IF NOT EXISTS idx_proposals_work ON proposals(work_item_id);
            PRAGMA user_version = 1;
            PRAGMA application_id = 1095911745;
            COMMIT;
        """)

    def close(self):
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _row(self, item_id):
        _text(item_id, "id")
        row = self._connection.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("Work item was not found")
        return row

    def _get_evidence(self, evidence_id):
        _text(evidence_id, "evidenceId")
        row = self._connection.execute("SELECT payload FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError("Evidence was not found")
        return json.loads(row["payload"])

    def _evidence_ids(self, evidence_ids):
        if evidence_ids is None:
            evidence_ids = []
        if not isinstance(evidence_ids, list):
            raise ValidationError("evidenceIds must be an array")
        for evidence_id in evidence_ids:
            _text(evidence_id, "evidenceId")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValidationError("evidenceIds must not contain duplicates")
        for evidence_id in evidence_ids:
            self._get_evidence(evidence_id)
        return list(evidence_ids)

    def _check_references(self, item):
        self._evidence_ids(item["sources"])
        sources = set(item["sources"])
        if not set(item["due"]["evidenceIds"]) <= sources:
            raise ValidationError("Due evidence must be included in sources")
        for entry in item["completionEvidence"]:
            if entry["evidenceId"] not in sources:
                raise ValidationError("Completion evidence must be included in sources")
            evidence = self._get_evidence(entry["evidenceId"])
            if evidence["role"] not in {"completion", "review_completion"}:
                raise ValidationError("Completion requires direct completion or reviewed completion evidence")
            if evidence["finality"] == "draft":
                raise ValidationError("Draft evidence cannot establish completion")
            _actor(entry["reviewedBy"], user=True)
        if item["status"] == "completed":
            conditions = {entry["id"] for entry in item["completionConditions"]}
            covered = {entry["conditionId"] for entry in item["completionEvidence"]}
            if not conditions or not conditions <= covered:
                raise ValidationError("Completed work requires reviewed evidence for every completion condition")

    @staticmethod
    def _risk(item, as_of):
        if as_of is None:
            today = datetime.now(timezone(timedelta(hours=9))).date()
        else:
            today = validate_date(as_of, "as_of")
        if item["status"] in {"completed", "cancelled"}:
            return "none"
        due = item["due"]
        if not due["confirmed"] or due["kind"] not in {"date", "window"}:
            return "unknown"
        deadline = validate_date(due["date"] if due["kind"] == "date" else due["endDate"])
        return "overdue" if deadline < today else "due_today" if deadline == today else "none"

    def _view(self, row, as_of=None):
        item = json.loads(row["payload"])
        return {**item, "version": row["version"], "confirmedFields": json.loads(row["confirmed_fields"]), "dueRisk": self._risk(item, as_of)}

    def _audit(self, entity_type, entity_id, event_type, actor_id, reason, evidence_ids, before, after, work_item_id=None):
        entry = {"entityType": entity_type, "entityId": entity_id, "workItemId": work_item_id,
                 "eventType": event_type, "actorId": actor_id,
                 "actorKind": "ai" if actor_id.strip().casefold() == "ai" or entity_type in {"extraction", "proposal"} and event_type != "proposal_resolved" else "system" if actor_id.strip().casefold() == "system" else "user",
                 "timestamp": _now(), "reason": reason,
                 "evidenceIds": list(evidence_ids), "before": before, "after": after}
        self._connection.execute("INSERT INTO audit_log(work_item_id,payload) VALUES (?,?)", (work_item_id, _json(entry)))

    def create_work_item(self, item, actor_id, reason, evidence_ids=None):
        _actor(actor_id, user=True)
        _text(reason, "reason")
        item = validate_work_item(item)
        with self._transaction():
            evidence_ids = self._evidence_ids(evidence_ids)
            self._check_references(item)
            if self._connection.execute("SELECT 1 FROM work_items WHERE id = ?", (item["id"],)).fetchone():
                raise ConflictError("Work item identity already exists")
            confirmed = sorted(set(item) & MUTABLE_FIELDS)
            self._connection.execute("INSERT INTO work_items VALUES (?,?,?,?)", (item["id"], _json(item), 1, _json(confirmed)))
            result = self._view(self._row(item["id"]))
            self._audit("work_item", item["id"], "work_item_created", actor_id, reason, evidence_ids, None, result, item["id"])
            return result

    def get_work_item(self, item_id, as_of=None):
        with self._lock:
            return self._view(self._row(item_id), as_of)

    def list_work_items(self, as_of=None):
        if as_of is not None:
            validate_date(as_of, "as_of")
        with self._lock:
            return [self._view(row, as_of) for row in self._connection.execute("SELECT * FROM work_items ORDER BY id")]

    def _update(self, item_id, patch, expected_version, actor_id, reason, evidence_ids):
        row = self._row(item_id)
        if row["version"] != expected_version:
            raise ConflictError("Work item version has changed; reload before editing")
        item = validate_work_item({**json.loads(row["payload"]), **patch})
        self._check_references(item)
        before = self._view(row)
        confirmed = sorted(set(json.loads(row["confirmed_fields"])) | set(patch))
        self._connection.execute("UPDATE work_items SET payload=?,version=?,confirmed_fields=? WHERE id=?", (_json(item), expected_version + 1, _json(confirmed), item_id))
        after = self._view(self._row(item_id))
        self._audit("work_item", item_id, "work_item_updated", actor_id, reason, evidence_ids, before, after, item_id)
        return after

    def update_work_item(self, item_id, patch, expected_version, actor_id, reason, evidence_ids=None):
        _actor(actor_id, user=True)
        _text(reason, "reason")
        _version(expected_version)
        patch = _patch(patch)
        with self._transaction():
            return self._update(item_id, patch, expected_version, actor_id, reason, self._evidence_ids(evidence_ids))

    def add_evidence(self, evidence, actor_id="user"):
        _actor(actor_id)
        evidence = validate_evidence(evidence)
        serialized = _json(evidence)
        with self._transaction():
            row = self._connection.execute("SELECT payload FROM evidence WHERE id=?", (evidence["id"],)).fetchone()
            if row:
                if row["payload"] == serialized:
                    return json.loads(row["payload"])
                raise ConflictError("Evidence is immutable; identity already has different content")
            fingerprint = evidence["sha256"] or ""
            duplicate = self._connection.execute("SELECT 1 FROM evidence WHERE provider=? AND source_id=? AND fingerprint=? AND locator=?", (evidence["provider"], evidence["sourceId"], fingerprint, evidence["locator"])).fetchone()
            if duplicate:
                raise ConflictError("Evidence source identity is already registered")
            self._connection.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?)", (evidence["id"], evidence["provider"], evidence["sourceId"], fingerprint, evidence["locator"], serialized))
            self._audit("evidence", evidence["id"], "evidence_added", actor_id, "Immutable source metadata registered", [evidence["id"]], None, evidence)
            return deepcopy(evidence)

    def get_evidence(self, evidence_id):
        with self._lock:
            return self._get_evidence(evidence_id)

    def list_evidence(self):
        with self._lock:
            return [json.loads(row["payload"]) for row in self._connection.execute("SELECT payload FROM evidence ORDER BY id")]

    def register_fund(self, fund_id, name, aliases=(), actor_id="user"):
        _actor(actor_id, user=True)
        registry = FundRegistry()
        registry.register(fund_id, name, aliases)
        fund = registry.list_funds()[0]
        with self._transaction():
            row = self._connection.execute("SELECT payload FROM funds WHERE id=?", (fund_id,)).fetchone()
            if row:
                if json.loads(row["payload"]) == fund:
                    return json.loads(row["payload"])
                raise ConflictError("Fund identity is already registered")
            self._connection.execute("INSERT INTO funds VALUES (?,?)", (fund_id, _json(fund)))
            self._audit("fund", fund_id, "fund_registered", actor_id, "Canonical fund identity and reviewed aliases registered", [], None, fund)
            return fund

    def list_funds(self):
        with self._lock:
            return [json.loads(row["payload"]) for row in self._connection.execute("SELECT payload FROM funds ORDER BY id")]

    def resolve_fund(self, name):
        return FundRegistry(self.list_funds()).resolve(name)

    def record_extraction(self, extraction_id, source_ids, output, model_version, actor_id="ai"):
        _text(extraction_id, "extraction id")
        _text(model_version, "modelVersion")
        _actor(actor_id)
        _json(output)
        with self._transaction():
            source_ids = self._evidence_ids(source_ids)
            if not source_ids:
                raise ValidationError("Extraction requires source evidence")
            row = self._connection.execute("SELECT payload FROM extractions WHERE id=?", (extraction_id,)).fetchone()
            fields = {"id": extraction_id, "sourceIds": source_ids, "output": deepcopy(output), "modelVersion": model_version, "actorId": actor_id}
            if row:
                existing = json.loads(row["payload"])
                if all(existing[key] == value for key, value in fields.items()):
                    return existing
                raise ConflictError("Extraction identity already exists")
            record = {**fields, "capturedAt": _now()}
            self._connection.execute("INSERT INTO extractions VALUES (?,?)", (extraction_id, _json(record)))
            self._audit("extraction", extraction_id, "extraction_recorded", actor_id, "Extracted output stored separately", source_ids, None, record)
            return record

    def propose(self, item_id, patch, base_version, actor_id="ai", reason="", evidence_ids=None):
        _actor(actor_id)
        _text(reason, "reason", empty=True)
        _version(base_version)
        patch = _patch(patch)
        with self._transaction():
            row = self._row(item_id)
            if row["version"] != base_version:
                raise ConflictError("Proposal base version has changed")
            item = json.loads(row["payload"])
            candidate = validate_work_item({**item, **patch})
            self._check_references(candidate)
            evidence_ids = self._evidence_ids(evidence_ids)
            conflicts = sorted(key for key in patch if key in json.loads(row["confirmed_fields"]) and patch[key] != item.get(key))
            proposal = {"id": str(uuid4()), "workItemId": item_id, "patch": patch, "baseVersion": base_version,
                        "actorId": actor_id, "reason": reason, "evidenceIds": evidence_ids, "conflictFields": conflicts,
                        "status": "pending", "createdAt": _now(), "resolvedAt": None, "resolvedBy": None}
            self._connection.execute("INSERT INTO proposals VALUES (?,?,?)", (proposal["id"], item_id, _json(proposal)))
            self._audit("proposal", proposal["id"], "proposal_created", actor_id, reason, evidence_ids, None, proposal, item_id)
            return deepcopy(proposal)

    def list_proposals(self, item_id):
        with self._lock:
            self._row(item_id)
            return [json.loads(row["payload"]) for row in self._connection.execute("SELECT payload FROM proposals WHERE work_item_id=? ORDER BY rowid", (item_id,))]

    def resolve_proposal(self, proposal_id, decision, expected_version, actor_id, reason):
        _actor(actor_id, user=True)
        _text(reason, "reason")
        _version(expected_version)
        if not isinstance(decision, str) or decision not in {"accepted", "rejected"}:
            raise ValidationError("Decision must be accepted or rejected")
        _text(proposal_id, "proposal id")
        with self._transaction():
            row = self._connection.execute("SELECT payload FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise NotFoundError("Proposal was not found")
            proposal = json.loads(row["payload"])
            if proposal["status"] != "pending":
                raise ConflictError("Proposal was already resolved")
            item_row = self._row(proposal["workItemId"])
            if item_row["version"] != expected_version:
                raise ConflictError("Work item version has changed")
            if decision == "accepted":
                if proposal["baseVersion"] != expected_version:
                    raise ConflictError("Stale proposal must be regenerated against the latest version")
                self._update(proposal["workItemId"], proposal["patch"], expected_version, actor_id, reason, proposal["evidenceIds"])
            resolved = {**proposal, "status": decision, "resolvedAt": _now(), "resolvedBy": actor_id, "resolutionReason": reason}
            self._connection.execute("UPDATE proposals SET payload=? WHERE id=?", (_json(resolved), proposal_id))
            self._audit("proposal", proposal_id, "proposal_resolved", actor_id, reason, proposal["evidenceIds"], proposal, resolved, proposal["workItemId"])
            return resolved

    def audit(self, item_id=None):
        with self._lock:
            if item_id is None:
                rows = self._connection.execute("SELECT id,payload FROM audit_log ORDER BY id")
            else:
                self._row(item_id)
                rows = self._connection.execute("SELECT id,payload FROM audit_log WHERE work_item_id=? ORDER BY id", (item_id,))
            return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]

    def record_activity(self, item_id, event_type, evidence_ids, actor_id, reason):
        _actor(actor_id)
        _text(event_type, "eventType")
        _text(reason, "reason")
        with self._transaction():
            self._row(item_id)
            evidence_ids = self._evidence_ids(evidence_ids)
            activity = {"id": str(uuid4()), "workItemId": item_id, "eventType": event_type,
                        "evidenceIds": evidence_ids, "actorId": actor_id, "reason": reason, "timestamp": _now()}
            self._connection.execute("INSERT INTO activities VALUES (?,?,?)", (activity["id"], item_id, _json(activity)))
            self._audit("activity", activity["id"], "activity_recorded", actor_id, reason, evidence_ids, None, activity, item_id)
            return activity

    def backup(self, destination):
        destination = Path(destination)
        if self.path != ":memory:" and destination.resolve() == Path(self.path).resolve():
            raise ValidationError("Backup destination must differ from the source database")
        if destination.exists():
            raise ConflictError("Backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            # Exclusive create prevents accidentally overwriting a concurrent backup.
            with destination.open("xb"):
                pass
            connection = sqlite3.connect(str(destination))
            try:
                self._connection.backup(connection)
            except Exception:
                connection.close()
                destination.unlink(missing_ok=True)
                raise
            finally:
                connection.close()
