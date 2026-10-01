"""Classic Outlook COM through installed Windows PowerShell; no Graph/MCP/OAuth.

Dedicated STA child process prevents HTTP thread COM apartment conflicts.
"""
from datetime import timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile


SCRIPT = Path(__file__).parent/'windows'/'outlook.ps1'


def _run(mode, mailbox, start=None, end=None, attachment=None, run_id=None):
    if os.name != 'nt':
        raise RuntimeError('Classic Outlook requires Windows')
    if not mailbox or '@' not in mailbox or any(c in mailbox for c in '\r\n'):
        raise ValueError('An explicit sender mailbox is required')
    with tempfile.TemporaryDirectory(prefix='ariadne-outlook-') as directory:
        result_path = Path(directory)/'result.json'
        command = [str(Path(os.environ['SystemRoot'])/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'),
          '-NoProfile','-NonInteractive','-STA','-File',str(SCRIPT),'-Mode',mode,
          '-Mailbox',mailbox,'-ResultPath',str(result_path)]
        if start is not None:
            command += ['-StartUtc',start.astimezone(timezone.utc).isoformat(),
                        '-EndUtc',end.astimezone(timezone.utc).isoformat()]
        if attachment is not None:
            command += ['-Attachment',str(Path(attachment).resolve()),'-RunId',run_id]
        completed = subprocess.run(command,capture_output=True,timeout=180,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if completed.returncode or not result_path.exists():
            # COM output can contain bodies or identifiers. Never echo stderr.
            raise RuntimeError('Outlook COM failed; check Outlook login and programmatic access policy')
        return json.loads(result_path.read_text(encoding='utf-8-sig'))


def collect(mailbox, start, end):
    if start.tzinfo is None or end.tzinfo is None or start>=end:
        raise ValueError('An aware, positive report window is required')
    return _run('collect',mailbox,start,end)


def send_to_self(mailbox, attachment, run_id):
    if not Path(attachment).is_file() or not run_id:
        raise ValueError('Report and run ID are required')
    return _run('send',mailbox,attachment=attachment,run_id=run_id)

