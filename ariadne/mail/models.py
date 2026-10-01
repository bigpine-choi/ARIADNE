"""Strict validation of bounded, normalized mail captures from the connector."""

from copy import deepcopy
import json
import re

from ..models import ValidationError, _datetime


MESSAGE_FIELDS = {
    "id", "internetMessageId", "subject", "fromName", "fromAddress", "toRecipients",
    "receivedAt", "sentAt", "bodyText", "bodyTruncated", "recipientsTruncated",
    "hasAttachments", "conversationId", "lastModifiedAt", "changeKey", "isDraft",
    "attachments", "attachmentsTruncated",
}
ATTACHMENT_FIELDS = {
    "id", "name", "contentType", "size", "isInline", "kind", "lastModifiedAt", "state",
}
MAX_CAPTURE_BYTES = 2 * 1024 * 1024


def _object(value, keys, field):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError(f"{field} must contain exactly the supported fields")


def _text(value, field, maximum, *, empty=True):
    if (not isinstance(value, str) or len(value) > maximum or "\x00" in value
            or (not empty and not value.strip())):
        raise ValidationError(f"{field} must be a bounded string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"{field} must be valid Unicode text") from None


def _timestamp(value, field, *, empty=True):
    _text(value, field, 64, empty=empty)
    if value:
        _datetime(value, field)


def _bool(value, field):
    if type(value) is not bool:
        raise ValidationError(f"{field} must be a boolean")


def validate_capture(value):
    """Reject extras (including credentials/raw provider fields), never truncate."""
    _object(value, {"mailbox", "folder", "fetchedAt", "messages"}, "capture")
    _object(value["mailbox"], {"id", "username"}, "mailbox")
    mailbox = value["mailbox"]
    if not isinstance(mailbox["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", mailbox["id"]):
        raise ValidationError("Mailbox identity must be an opaque SHA-256 identity")
    _text(mailbox["username"], "mailbox username", 500, empty=False)
    if value["folder"] not in ("inbox", "sentitems"):
        raise ValidationError("Folder must be inbox or sentitems")
    _timestamp(value["fetchedAt"], "fetchedAt", empty=False)
    messages = value["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 10:
        raise ValidationError("Select between 1 and 10 messages")
    identities = set()
    for message in messages:
        local = message.get("localSource") if isinstance(message, dict) else None
        _object(message, MESSAGE_FIELDS | ({"localSource"} if local is not None else set()), "message")
        if local is not None:
            _object(local, {"kind", "folderId", "folderPath", "timeBasis"}, "localSource")
            if local["kind"] != "outlook_com" or local["timeBasis"] not in {"inbox", "sentitems"}:
                raise ValidationError("Invalid local source")
            if not isinstance(local["folderId"], str) or not re.fullmatch(r"[0-9a-f]{64}", local["folderId"]):
                raise ValidationError("Invalid local folder identity")
            _text(local["folderPath"], "folderPath", 1000, empty=False)
        for field, maximum in (("id", 2048), ("internetMessageId", 2048), ("subject", 2000),
                               ("fromName", 500), ("fromAddress", 500), ("bodyText", 100000),
                               ("conversationId", 2048), ("changeKey", 2048)):
            _text(message[field], field, maximum, empty=field not in {"id", "changeKey"})
        if message["id"] in identities:
            raise ValidationError("A capture must not contain duplicate message identities")
        identities.add(message["id"])
        for field in ("receivedAt", "sentAt", "lastModifiedAt"):
            _timestamp(message[field], field)
        for field in ("bodyTruncated", "recipientsTruncated", "hasAttachments", "isDraft", "attachmentsTruncated"):
            _bool(message[field], field)
        recipients = message["toRecipients"]
        if not isinstance(recipients, list) or len(recipients) > 100:
            raise ValidationError("Recipient metadata exceeds the supported bound")
        for recipient in recipients:
            _object(recipient, {"name", "address"}, "recipient")
            for field in ("name", "address"):
                _text(recipient[field], f"recipient {field}", 500)
        attachments = message["attachments"]
        if not isinstance(attachments, list) or len(attachments) > 100:
            raise ValidationError("Attachment metadata exceeds the supported bound")
        attachment_ids = set()
        for attachment in attachments:
            _object(attachment, ATTACHMENT_FIELDS, "attachment")
            for field, maximum in (("id", 2048), ("name", 2000), ("contentType", 500)):
                _text(attachment[field], f"attachment {field}", maximum, empty=field != "id")
            if attachment["id"] in attachment_ids:
                raise ValidationError("Attachment metadata contains duplicate identities")
            attachment_ids.add(attachment["id"])
            size = attachment["size"]
            if type(size) is not int or not 0 <= size <= 2**63 - 1:
                raise ValidationError("Attachment size must be a bounded nonnegative integer")
            _bool(attachment["isInline"], "attachment isInline")
            if attachment["kind"] not in ("file", "item", "reference", "unknown"):
                raise ValidationError("Attachment kind is unsupported")
            if attachment["state"] != "not_downloaded":
                raise ValidationError("Only attachment metadata may be imported")
            _timestamp(attachment["lastModifiedAt"], "attachment lastModifiedAt")
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValidationError("Capture must contain finite JSON data") from None
    if size > MAX_CAPTURE_BYTES:
        raise ValidationError("Selected mail metadata exceeds the capture size limit")
    normalized = deepcopy(value)
    for message in normalized["messages"]:
        # Graph attachment pagination does not promise presentation order.
        # An order-only change must not manufacture another evidence revision.
        message["attachments"].sort(key=lambda attachment: attachment["id"])
    return normalized
