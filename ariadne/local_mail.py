"""Classic Outlook COM through installed Windows PowerShell; no Graph/MCP/OAuth.

Dedicated STA child process prevents HTTP thread COM apartment conflicts.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .native_powershell import NativeLaunchError, script_command, failure_code, require_ready, child_environment


SCRIPT = Path(__file__).parent/'windows'/'outlook.ps1'


def preflight():
    """No COM activation; safe to call before recording a send claim."""
    return require_ready(SCRIPT, require_outlook=True)


def _run(mode, mailbox, start=None, end=None, attachment=None, run_id=None,
         attachment_sha256=None):
    if os.name != 'nt':
        raise RuntimeError('Classic Outlook requires Windows')
    if not mailbox or '@' not in mailbox or any(c in mailbox for c in '\r\n'):
        raise ValueError('An explicit sender mailbox is required')
    with tempfile.TemporaryDirectory(prefix='ariadne-outlook-') as directory:
        result_path = Path(directory)/'result.json'
        command = script_command(SCRIPT, ['-Mode',mode,
          '-Mailbox',mailbox,'-ResultPath',str(result_path)], require_outlook=True)
        if start is not None:
            command += ['-StartUtc',start.astimezone(timezone.utc).isoformat(),
                        '-EndUtc',end.astimezone(timezone.utc).isoformat()]
        if attachment is not None:
            command += ['-Attachment',str(Path(attachment).resolve()),'-RunId',run_id]
        if attachment_sha256 is not None:
            command += ['-AttachmentSha256',attachment_sha256]
        try:
            completed = subprocess.run(command,capture_output=True,timeout=180,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env=child_environment())
        except subprocess.TimeoutExpired:
            raise NativeLaunchError('NATIVE_COLLECTION_TIMEOUT') from None
        except OSError:
            raise NativeLaunchError('OUTLOOK_COM_UNAVAILABLE') from None
        if completed.returncode or not result_path.exists():
            # COM output can contain bodies or identifiers. Never echo stderr.
            raise NativeLaunchError(failure_code(completed, outlook=True)) from None
        try:
            return json.loads(result_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise NativeLaunchError('OUTLOOK_COM_UNAVAILABLE') from None


def collect(mailbox, start, end):
    if start.tzinfo is None or end.tzinfo is None or start>=end:
        raise ValueError('An aware, positive report window is required')
    return _run('collect',mailbox,start,end)


def send_to_self(mailbox, attachment, run_id):
    if not Path(attachment).is_file() or not run_id:
        raise ValueError('Report and run ID are required')
    return _run('send',mailbox,attachment=attachment,run_id=run_id)


def confirm_delivery(mailbox, attachment, run_id, since):
    """Read the configured Inbox; a matching attachment is required for delivery.

    This only inspects locally available Outlook items. Absence is pending, not
    proof of failed delivery, and must never be used to trigger a resend.
    """
    report = Path(attachment)
    if not report.is_file() or report.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('A report of at most 50 MiB is required')
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', run_id):
        raise ValueError('A valid report run ID is required')
    end = datetime.now(timezone.utc)
    if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
        raise ValueError('An aware delivery window is required')
    start = since.astimezone(timezone.utc)
    if start >= end or end - start > timedelta(days=30):
        raise ValueError('Delivery checks require a positive window of at most 30 days')
    digest = hashlib.sha256()
    with report.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    try:
        payload = _run('confirm', mailbox, start, end, attachment=report,
                       run_id=run_id, attachment_sha256=digest.hexdigest())
        if not isinstance(payload, dict):
            raise ValueError
        status = payload.get('status')
        reason = payload.get('reason')
        reasons = {'matched', 'not_found', 'scan_limit', 'scan_incomplete', 'attachment_mismatch'}
        if status not in {'confirmed', 'pending', 'review_required'} or reason not in reasons:
            raise ValueError
        delivered = status == 'confirmed'
        if payload.get('delivered') is not delivered:
            raise ValueError
        received_at = payload.get('receivedAt')
        receipt_id = payload.get('id')
        if delivered:
            if reason != 'matched' or not isinstance(received_at, str):
                raise ValueError
            received = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
            if received.tzinfo is None or not start <= received <= end:
                raise ValueError
            if not isinstance(receipt_id, str) or not re.fullmatch(r'[0-9a-f]{64}', receipt_id):
                raise ValueError
            received_at = received.astimezone(timezone.utc).isoformat()
        else:
            if received_at is not None or receipt_id is not None or reason == 'matched':
                raise ValueError
            if status == 'pending' and reason != 'not_found':
                raise ValueError
        # Do not return identifiers, addresses, subjects or bodies from COM.
        return {'status': status, 'delivered': delivered, 'receivedAt': received_at,
                'id': receipt_id, 'reason': reason}
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Outlook delivery verification failed; check local Outlook availability') from None
