"""Durable Friday reporting and explicit verification of an approved report.

Submission and Inbox receipt are separate. Uncertain sends are never retried.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile

from . import local_mail
from .config import data_directory
from .reporting import extract_report, report_window, write_report, validate_report

VALIDATION_ID = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{12}-validation-[0-9a-f-]{36}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _get_run(store, identifier):
    with store.lock:
        row = store.connection.execute("SELECT * FROM weekly_runs WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise ValueError("Report run not found")
    run = dict(row)
    run["detail"] = json.loads(run["detail"])
    return run


def _public(run, status=None):
    detail = run["detail"]
    return {
        "id": run["id"], "status": status or run["status"], "runStatus": run["status"],
        "period": {"start": run["start"], "end": run["end"]},
        **{k: detail[k] for k in (
            "recipient", "outputHash", "outputVersionId", "warnings", "limitations",
            "receivedAt", "deliveryReason", "hiworksDraftId", "nativePreflightCode") if k in detail},
    }


def _validation_run(store, settings, identifier):
    if not isinstance(identifier, str) or not VALIDATION_ID.fullmatch(identifier):
        raise ValueError("An operational validation run is required")
    run = _get_run(store, identifier)
    owner = settings["mailbox"].strip().casefold()
    detail = run["detail"]
    if (not owner or detail.get("recipient") != owner
            or detail.get("kind") != "operational_validation"):
        raise ValueError("Report owner or type changed")
    expected = hashlib.sha256(owner.encode()).hexdigest()[:12]
    if identifier.split("-validation-", 1)[0] != (
            datetime.fromisoformat(run["end"]).strftime("%Y-%m-%d") + "-" + expected):
        raise ValueError("Report owner identity mismatch")
    return run


def _artifact(store, run, require_local=False):
    detail = run["detail"]
    digest = detail.get("outputHash", "")
    if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
            or type(detail.get("outputVersionId")) is not int):
        raise ValueError("Report integrity metadata is missing")
    content = store.read_version(detail["outputVersionId"])
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Archived report changed")
    output = Path(run["output"]).resolve()
    root = (data_directory() / "reports").resolve()
    if not output.is_relative_to(root) or output.suffix != ".xlsx":
        raise ValueError("Report path is outside the report directory")
    if require_local and (not output.is_file()
            or hashlib.sha256(output.read_bytes()).hexdigest() != digest):
        raise ValueError("Reviewed report changed; approval is invalid")
    return output, content


def _send_once(store, run, sender, stage=True):
    identifier = run["id"]
    detail = dict(run["detail"])
    output = Path(run["output"])
    # The attachment handed to Outlook is a private copy of the pinned archive.
    # A later edit of the download/preparation file cannot change approved bytes.
    try:
        _, content = _artifact(store, run)
        if stage:
            with tempfile.TemporaryDirectory(prefix="ariadne-approved-report-") as directory:
                attachment = Path(directory) / output.name
                attachment.write_bytes(content)
                receipt = sender(detail["recipient"], attachment, identifier)
        else:
            _artifact(store, run, require_local=True)
            receipt = sender(detail["recipient"], output, identifier)
        if not isinstance(receipt, dict) or receipt.get("submitted") is not True:
            raise RuntimeError("Outlook submission was not acknowledged")
    except Exception:
        store.set_run(identifier, "delivery_unknown", output, detail,
                      preserve_statuses=("delivered",), merge_detail=True)
        return _public(_get_run(store, identifier))
    store.set_run(identifier, "submitted", output, {**detail, "delivered": False},
                  preserve_statuses=("delivered",), merge_detail=True)
    return _public(_get_run(store, identifier))


def send_prepared(store, settings, identifier, expected_hash, sender=None):
    """An explicit approval binds owner, run and immutable artifact, once only."""
    if not isinstance(expected_hash, str) or not DIGEST.fullmatch(expected_hash):
        raise ValueError("An exact approved file hash is required")
    run = _validation_run(store, settings, identifier)
    if run["status"] != "prepared" or run["detail"].get("outputHash") != expected_hash:
        raise ValueError("Report was changed or is no longer awaiting approval")
    _artifact(store, run, require_local=True)
    if sender is None:
        # A known installation/policy failure has not attempted any delivery.
        # Keep the reviewed report prepared instead of consuming its send claim.
        local_mail.preflight()
    # Recheck/claim atomically across UI, scheduler and other worker processes.
    with store.transaction() as db:
        current = db.execute("SELECT * FROM weekly_runs WHERE id=?", (identifier,)).fetchone()
        if current is None or current["status"] != "prepared":
            raise ValueError("Report sending was already claimed")
        detail = json.loads(current["detail"])
        if (detail.get("outputHash") != expected_hash
                or detail.get("recipient") != settings["mailbox"].strip().casefold()
                or detail.get("kind") != "operational_validation"
                or detail.get("limitations") and not detail.get("scopeAcknowledged")):
            raise ValueError("Approval no longer matches the report")
        stamp = datetime.now(timezone.utc).isoformat()
        detail.update({"approvedHash": expected_hash, "approvedAt": stamp,
                       "approvedBy": "local_user", "sendingStartedAt": stamp})
        db.execute("UPDATE weekly_runs SET status='sending',detail=? WHERE id=?",
                   (json.dumps(detail, ensure_ascii=False), identifier))
    return _send_once(store, _get_run(store, identifier), sender or local_mail.send_to_self)


def confirm_delivery_run(store, settings, identifier, confirmer=None):
    """Read only in Outlook; absence/limits never release the send claim."""
    run = _validation_run(store, settings, identifier)
    if run["status"] == "delivered":
        return _public(run)
    if run["status"] not in {"sending", "submitted", "delivery_unknown"}:
        raise ValueError("Only a submitted or uncertain report can be checked")
    detail = dict(run["detail"])
    _, content = _artifact(store, run)
    stamp = datetime.fromisoformat(detail["sendingStartedAt"])
    if stamp.tzinfo is None:
        raise ValueError("Aware submission timestamp required")
    since = stamp.astimezone(timezone.utc).replace(microsecond=0)
    try:
        with tempfile.TemporaryDirectory(prefix="ariadne-receipt-report-") as directory:
            attachment = Path(directory) / Path(run["output"]).name
            attachment.write_bytes(content)
            receipt = (confirmer or local_mail.confirm_delivery)(
                detail["recipient"], attachment, identifier, since)
    except Exception:
        receipt = {"status": "review_required", "delivered": False,
                   "reason": "receipt_lookup_unavailable"}
    if not isinstance(receipt, dict):
        receipt = {"status": "review_required", "delivered": False,
                   "reason": "receipt_response_invalid"}
    result_status = receipt.get("status")
    confirmed = result_status == "confirmed" and receipt.get("delivered") is True
    received = None
    if confirmed:
        try:
            received = datetime.fromisoformat(receipt["receivedAt"])
            if received.tzinfo is None or received < since or received > datetime.now(timezone.utc) + timedelta(minutes=5):
                confirmed = False
        except (KeyError, ValueError, TypeError):
            confirmed = False
    receipt_detail = {"lastDeliveryCheckedAt": datetime.now(timezone.utc).isoformat()}
    reason = receipt.get("reason", "receipt_unverified")
    receipt_detail["deliveryReason"] = reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,100}", reason) else "receipt_unverified"
    if confirmed:
        receipt_id = receipt.get("id")
        receipt_detail.update({"receivedAt": received.astimezone(timezone.utc).isoformat(),
                       "delivered": True, "receiptId": receipt_id if isinstance(receipt_id, str) and DIGEST.fullmatch(receipt_id) else None})
        with store.transaction() as db:
            current = db.execute("SELECT status,detail FROM weekly_runs WHERE id=?", (identifier,)).fetchone()
            if current["status"] == "delivered":
                return _public(_get_run(store, identifier))
            if current["status"] not in {"sending", "submitted", "delivery_unknown"}:
                raise ValueError("Delivery status changed")
            detail = {**json.loads(current["detail"]), **receipt_detail}
            db.execute("UPDATE weekly_runs SET status='delivered',detail=? WHERE id=?",
                       (json.dumps(detail, ensure_ascii=False), identifier))
        return _public(_get_run(store, identifier))
    # A failed lookup never changes submitted/uncertain into retryable failed.
    with store.transaction() as db:
        current = db.execute("SELECT status,detail FROM weekly_runs WHERE id=?", (identifier,)).fetchone()
        if current["status"] == "delivered":
            return _public(_get_run(store, identifier))
        if current["status"] not in {"sending", "submitted", "delivery_unknown"}:
            raise ValueError("Delivery status changed")
        detail = {**json.loads(current["detail"]), **receipt_detail}
        db.execute("UPDATE weekly_runs SET detail=? WHERE id=?",
                   (json.dumps(detail, ensure_ascii=False), identifier))
    public_status = "delivery_pending" if result_status == "pending" else "delivery_review_required"
    return _public(_get_run(store, identifier), public_status)


def run_weekly(store, settings, at=None, mail_collector=None, teams_collector=None,
               extractor=None, sender=None, extra_sources=None, identifier_suffix="",
               template_bytes=None, strict_sources=False, scope_acknowledged=False):
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Aware report time required")
    if type(strict_sources) is not bool or type(scope_acknowledged) is not bool:
        raise ValueError("Source policy switches must be boolean")
    if not settings["enabled"]:
        return {"status": "disabled"}
    mailbox = settings["mailbox"].strip().casefold()
    if not mailbox:
        raise ValueError("Configure the owner mailbox first")
    start, end = report_window(at)
    suffix = hashlib.sha256(mailbox.encode()).hexdigest()[:12]
    identifier = f"{end:%Y-%m-%d}-{suffix}" + ("-" + identifier_suffix if identifier_suffix else "")
    if not store.claim_run(identifier, start.isoformat(), end.isoformat()):
        return {"id": identifier, "status": "already_claimed"}
    operational = identifier_suffix.startswith("validation-")
    if operational and (not strict_sources or settings["auto_send"]):
        store.set_run(identifier, "failed", detail={"reason": "validation_must_be_unsent_and_strict"})
        raise ValueError("Operational validation cannot send during preparation")
    warnings, limitations = [], []
    output, output_version = None, None
    delivery_started = False
    common = {"recipient": mailbox,
              "kind": "operational_validation" if operational else ("preview" if identifier_suffix else "weekly"),
              "scopeAcknowledged": scope_acknowledged}
    try:
        mail = (mail_collector or local_mail.collect)(mailbox, start, end)
        warnings += mail.get("warnings", [])
        limitations += mail.get("limitations", [])
        if not mail.get("complete"):
            warnings.append("mail_collection_incomplete")
        messages = mail["messages"]
        for message in messages:
            stamp = datetime.fromisoformat(message["timestamp"])
            if stamp.tzinfo is None or not start <= stamp < end:
                raise ValueError("Collected mail timestamp is outside the report period")
        if not mail.get("archiveSnapshot"):
            store.ingest_messages("outlook:" + mailbox, messages)
        try:
            teams = teams_collector(settings["teams_chats"]) if teams_collector else {
                "messages": [], "complete": not settings["teams_chats"],
                "warnings": ["teams_archive_required"] if settings["teams_chats"] else []}
        except RuntimeError:
            teams = {"messages": [], "complete": False, "warnings": ["teams_local_collection_unavailable"]}
        warnings += teams.get("warnings", [])
        limitations += teams.get("limitations", [])
        if not teams.get("complete"):
            warnings.append("teams_collection_incomplete")
        if not teams.get("archiveSnapshot"):
            store.ingest_messages("teams:" + mailbox, teams["messages"])
        messages = messages + teams["messages"]
        if extra_sources:
            context_result = extra_sources(start, end)
            context, context_warnings = context_result[:2]
            context_limitations = context_result[2] if len(context_result) == 3 else []
            messages += context
            warnings += context_warnings
            limitations += context_limitations
        warnings = list(dict.fromkeys(warnings))
        limitations = list(dict.fromkeys(limitations))
        common.update({"warnings": warnings, "limitations": limitations})
        if warnings and (strict_sources or not settings["allow_partial_report"]):
            store.set_run(identifier, "failed", detail={**common, "reason": "incomplete_sources"})
            return _public(_get_run(store, identifier), "incomplete_sources")
        if limitations and not (scope_acknowledged or (not strict_sources and settings["allow_partial_report"])):
            store.set_run(identifier, "failed", detail={**common, "reason": "scope_confirmation_required"})
            return _public(_get_run(store, identifier), "scope_confirmation_required")
        if not messages:
            raise ValueError("No current evidence; report generation is blocked")
        if all(message.get("contextOnly") for message in messages):
            raise ValueError("Current-period evidence is required")
        report, sources = (extractor or extract_report)(messages, settings["model"], settings["max_input_chars"])
        if any(source.get("analysisChunks", 1) > 1 for source in sources):
            warnings.append("ai_input_processed_in_chunks")
        validate_report(report, {source["evidence_id"] for source in sources})
        output = data_directory() / "reports" / f"weekly-{identifier}.xlsx"
        write_report(output, report, sources, start, end, warnings + limitations)
        if settings.get("report_template"):
            from .meeting_report import write_meeting_report
            write_meeting_report(output, template_bytes if template_bytes is not None else settings["report_template"],
                                 report, sources, start, end, warnings + limitations)
        output_content = output.read_bytes()
        output_version = store.archive_bytes("reports:" + mailbox, output.name, output_content, {"kind": "generated_report"})
        from .hiworks import DraftStore, prepare_weekly
        draft = DraftStore(data_directory() / "hiworks.sqlite3").save(
            prepare_weekly(report, sources, start, end, warnings + limitations))
        common.update({"outputVersionId": output_version, "outputHash": hashlib.sha256(output_content).hexdigest(),
                       "hiworksDraftId": draft["id"]})
        store.set_run(identifier, "prepared", output, common)
        if not settings["auto_send"]:
            return {**_public(_get_run(store, identifier)), "output": str(output)}
        if sender is None:
            try:
                local_mail.preflight()
            except local_mail.NativeLaunchError as error:
                store.set_run(identifier, "prepared", output,
                              {**common, "nativePreflightCode": error.code})
                return {**_public(_get_run(store, identifier)), "output": str(output),
                        "nativePreflightCode": error.code}
        common["sendingStartedAt"] = datetime.now(timezone.utc).isoformat()
        store.set_run(identifier, "sending", output, common)
        delivery_started = True
        result = _send_once(store, _get_run(store, identifier), sender or local_mail.send_to_self, stage=False)
        return {**result, "output": str(output)}
    except Exception as error:
        store.set_run(identifier, "delivery_unknown" if delivery_started else "failed", output,
                      {**common, "error_type": type(error).__name__},
                      preserve_statuses=("delivered",), merge_detail=delivery_started)
        raise


def scheduled_tick(store, settings, at=None, **dependencies):
    at = at or datetime.now(timezone.utc)
    _, end = report_window(at)
    if at < end + timedelta(minutes=15) or (at - end).total_seconds() > 3 * 24 * 3600:
        return {"status": "not_due"}
    return run_weekly(store, settings, at, **dependencies)
