"""Strict, dependency-free validation of the v0.1 persistence contract."""

from copy import deepcopy
from datetime import date, datetime
import re


class ValidationError(ValueError):
    """The supplied public data does not satisfy the v0.1 contract."""


STATUS_LABELS = {
    "new": "신규", "needs_review": "검토 필요", "in_progress": "진행 중",
    "waiting_internal": "내부 승인 대기", "waiting_external": "외부 회신 대기",
    "waiting_schedule": "일정 확정 대기", "completed": "완료", "on_hold": "보류",
    "overdue": "기한 초과", "reopened": "재개", "cancelled": "취소",
}

ALLOWED_FIELDS = {
    "id", "title", "fundId", "ownerId", "status", "reviewRequired", "confidence",
    "due", "sources", "completionConditions", "completionEvidence", "waitingReason", "assetId",
}
REQUIRED_FIELDS = ALLOWED_FIELDS - {"waitingReason", "assetId"}
MUTABLE_FIELDS = ALLOWED_FIELDS - {"id"}


def _object(value, allowed, required, path):
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must be an object")
    if set(value) - allowed:
        raise ValidationError(f"{path} contains unsupported fields")
    if required - set(value):
        raise ValidationError(f"{path} is missing required fields")


def _string(value, path, *, nonempty=True, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValidationError(f"{path} must be {'a nonempty' if nonempty else 'a'} string")
    return value


def _bool(value, path):
    if type(value) is not bool:
        raise ValidationError(f"{path} must be a boolean")


def _integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{path} must be an integer >= {minimum}")


def _enum(value, values, path):
    if not isinstance(value, str) or value not in values:
        raise ValidationError(f"{path} has an unsupported value")


def validate_date(value, path="date"):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError(f"{path} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{path} must be a valid calendar date") from None


def _datetime(value, path):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValidationError(f"{path} must be an ISO timestamp with a timezone offset")
    if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValidationError(f"{path} must have a valid timezone offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValidationError(f"{path} must be a valid offset timestamp") from None


def _ids(value, path):
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array")
    for entry in value:
        _string(entry, path)
    if len(value) != len(set(value)):
        raise ValidationError(f"{path} contains duplicate identities")


def validate_work_item(value):
    """Return a detached validated work item, without repository metadata."""
    item = deepcopy(value)
    _object(item, ALLOWED_FIELDS, REQUIRED_FIELDS, "work item")
    for key in ("id", "title"):
        _string(item[key], key)
    for key in ("fundId", "ownerId"):
        _string(item[key], key, nullable=True)
    if "assetId" in item:
        _string(item["assetId"], "assetId", nullable=True)
    if "waitingReason" in item:
        _string(item["waitingReason"], "waitingReason", nonempty=False)
    _enum(item["status"], STATUS_LABELS, "status")
    _enum(item["confidence"], {"high", "medium", "low"}, "confidence")
    _bool(item["reviewRequired"], "reviewRequired")
    _ids(item["sources"], "sources")

    due = item["due"]
    required = {"kind", "rawText", "date", "timezone", "anchorTaskId", "confirmed", "evidenceIds"}
    allowed = required | {"startDate", "endDate", "rule", "preparationDays"}
    _object(due, allowed, required, "due")
    _enum(due["kind"], {"date", "window", "recurrence", "event", "unknown"}, "due.kind")
    _string(due["rawText"], "due.rawText", nonempty=False)
    if due["date"] is not None:
        validate_date(due["date"], "due.date")
    _enum(due["timezone"], {"Asia/Seoul"}, "due.timezone")
    _string(due["anchorTaskId"], "due.anchorTaskId", nullable=True)
    _bool(due["confirmed"], "due.confirmed")
    _ids(due["evidenceIds"], "due.evidenceIds")
    for key in ("startDate", "endDate"):
        if key in due:
            validate_date(due[key], f"due.{key}")
    if "rule" in due:
        _string(due["rule"], "due.rule")
    if "preparationDays" in due:
        _integer(due["preparationDays"], "due.preparationDays")
    if due["confirmed"] and not due["evidenceIds"]:
        raise ValidationError("Confirmed due requires evidenceIds")
    if due["kind"] == "date" and due["date"] is None:
        raise ValidationError("Date due requires date")
    if due["kind"] == "unknown" and (due["date"] is not None or due["confirmed"]):
        raise ValidationError("Unknown due must be undated and unconfirmed")
    if due["kind"] == "window":
        if not {"startDate", "endDate"} <= set(due):
            raise ValidationError("Window due requires startDate and endDate")
        if due["startDate"] > due["endDate"]:
            raise ValidationError("Window startDate must not follow endDate")
    if due["kind"] == "recurrence" and ("rule" not in due or due["confirmed"]):
        raise ValidationError("Recurrence requires rule and remains unconfirmed in v0.1")
    if due["kind"] == "event" and (
        not due["anchorTaskId"] or due["date"] is not None or due["confirmed"]
    ):
        raise ValidationError("Event due requires anchorTaskId and remains undated and unconfirmed")

    conditions = item["completionConditions"]
    entries = item["completionEvidence"]
    if not isinstance(conditions, list) or not isinstance(entries, list):
        raise ValidationError("Completion conditions and evidence must be arrays")
    condition_ids = set()
    for entry in conditions:
        _object(entry, {"id", "label"}, {"id", "label"}, "completion condition")
        _string(entry["id"], "completion condition.id")
        _string(entry["label"], "completion condition.label")
        if entry["id"] in condition_ids:
            raise ValidationError("Duplicate completion condition")
        condition_ids.add(entry["id"])
    seen = set()
    for entry in entries:
        keys = {"conditionId", "evidenceId", "reviewedBy", "reviewedAt"}
        _object(entry, keys, keys, "completion evidence")
        for key in ("conditionId", "evidenceId", "reviewedBy"):
            _string(entry[key], f"completion evidence.{key}")
        _datetime(entry["reviewedAt"], "completion evidence.reviewedAt")
        if entry["conditionId"] not in condition_ids:
            raise ValidationError("Completion evidence refers to an unknown condition")
        identity = (entry["conditionId"], entry["evidenceId"])
        if identity in seen:
            raise ValidationError("Duplicate completion evidence")
        seen.add(identity)
    return item


def validate_evidence(value):
    """Validate immutable evidence metadata; original source files stay untouched."""
    evidence = deepcopy(value)
    required = {"id", "provider", "sourceId", "capturedAt", "evidence", "locator", "sha256", "threadId", "finality", "role"}
    _object(evidence, required | {"page", "clause"}, required, "evidence")
    for key in ("id", "sourceId", "evidence", "locator"):
        _string(evidence[key], key)
    _enum(evidence["provider"], {"manual", "outlook", "document", "meeting", "synthetic"}, "provider")
    _datetime(evidence["capturedAt"], "capturedAt")
    _string(evidence["threadId"], "threadId", nullable=True)
    _enum(evidence["finality"], {"draft", "signed", "final", "unknown"}, "finality")
    _enum(evidence["role"], {"context", "request", "email_sent", "attachment_received", "completion", "review_completion"}, "role")
    sha = evidence["sha256"]
    if sha is not None:
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise ValidationError("sha256 must be null or a 64-digit hexadecimal digest")
        evidence["sha256"] = sha.lower()
    if "page" in evidence:
        _integer(evidence["page"], "page", 1)
    if "clause" in evidence:
        _string(evidence["clause"], "clause")
    return evidence

