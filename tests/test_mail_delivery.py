"""Delivery verification tests never open Outlook or send a message."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ariadne import local_mail, native_powershell


class DeliveryTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.report = Path(self.directory.name) / 'weekly.xlsx'
        self.report.write_bytes(b'approved-report-bytes')
        self.since = datetime.now(timezone.utc) - timedelta(minutes=2)
        # This transport contract uses synthetic processes, never the host's
        # actual Outlook registration/policy. Preflight has dedicated tests.
        for fixture in (
                patch.object(native_powershell, 'diagnostics', return_value={
                    'ready':True,'codes':[],'processPolicyRequested':'RemoteSigned'}),
                patch.object(native_powershell, 'powershell_executable', return_value=
                    Path('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'))):
            fixture.start()
            self.addCleanup(fixture.stop)

    @staticmethod
    def pending(reason='not_found', status='pending'):
        return {'status': status, 'delivered': False, 'reason': reason,
                'receivedAt': None, 'id': None}

    def test_read_only_process_contract_pins_identity_and_digest(self):
        seen = []

        def fake_process(command, **kwargs):
            seen.append(command)
            self.assertTrue(kwargs['capture_output'])
            self.assertEqual(kwargs['timeout'], 180)
            result_path = Path(command[command.index('-ResultPath') + 1])
            result_path.write_text(json.dumps(self.pending()), encoding='utf-8')
            return SimpleNamespace(returncode=0)

        # Replace this module's os reference, without changing pathlib's host OS.
        windows = SimpleNamespace(name='nt', environ={'SystemRoot': 'C:/Windows'})
        with patch.object(local_mail, 'os', windows), patch.object(local_mail.subprocess, 'run', fake_process):
            result = local_mail.confirm_delivery('owner@example.test', self.report, 'week-01', self.since)
        command = seen[0]
        self.assertEqual(command[command.index('-Mode') + 1], 'confirm')
        self.assertEqual(command[command.index('-Mailbox') + 1], 'owner@example.test')
        self.assertEqual(command[command.index('-RunId') + 1], 'week-01')
        self.assertEqual(command[command.index('-Attachment') + 1], str(self.report.resolve()))
        self.assertEqual(command[command.index('-AttachmentSha256') + 1],
                         hashlib.sha256(self.report.read_bytes()).hexdigest())
        self.assertEqual(datetime.fromisoformat(command[command.index('-StartUtc') + 1]), self.since)
        self.assertIn('-STA', command)
        self.assertIn('-NonInteractive', command)
        self.assertNotIn('send', command)
        self.assertEqual(result, self.pending())

    def test_confirmation_only_returns_sanitized_receipt(self):
        received = self.since + timedelta(seconds=1)
        payload = {'status': 'confirmed', 'delivered': True, 'reason': 'matched',
                   'receivedAt': received.isoformat(), 'id': 'a' * 64,
                   'body': 'private body', 'sender': 'private address', 'EntryID': 'private id'}
        with patch.object(local_mail, '_run', return_value=payload):
            result = local_mail.confirm_delivery('owner@example.test', self.report, 'week-01', self.since)
        self.assertEqual(set(result), {'status', 'delivered', 'reason', 'receivedAt', 'id'})
        self.assertEqual(result['receivedAt'], received.isoformat())
        self.assertNotIn('private', json.dumps(result))

    def test_unreadable_and_mismatched_candidates_require_review(self):
        for reason in ('scan_limit', 'scan_incomplete', 'attachment_mismatch'):
            with self.subTest(reason=reason), patch.object(local_mail, '_run',
                    return_value=self.pending(reason, 'review_required')):
                result = local_mail.confirm_delivery('owner@example.test', self.report, 'week-01', self.since)
                self.assertFalse(result['delivered'])
                self.assertEqual(result['status'], 'review_required')

    def test_invalid_confirmation_cannot_become_delivered(self):
        base = {'status': 'confirmed', 'delivered': True, 'reason': 'matched',
                'receivedAt': (self.since + timedelta(seconds=1)).isoformat(), 'id': 'a' * 64}
        bad = [None, [], {**base, 'delivered': False}, {**base, 'receivedAt': 'not a date'},
               {**base, 'receivedAt': (self.since - timedelta(seconds=1)).isoformat()},
               {**base, 'id': 'raw outlook id'}, {**base, 'reason': 'mail body'},
               {**self.pending(), 'receivedAt': base['receivedAt']},
               self.pending('scan_incomplete')]
        for payload in bad:
            with self.subTest(payload=payload), patch.object(local_mail, '_run', return_value=payload):
                with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                    local_mail.confirm_delivery('owner@example.test', self.report, 'week-01', self.since)

    def test_invalid_inputs_never_start_a_process(self):
        with patch.object(local_mail, '_run') as runner:
            for since in (datetime.now(), datetime.now(timezone.utc) + timedelta(days=1),
                          datetime.now(timezone.utc) - timedelta(days=31)):
                with self.subTest(since=since), self.assertRaises(ValueError):
                    local_mail.confirm_delivery('owner@example.test', self.report, 'week-01', since)
            for run_id in ('', 'week\n01', "week'01", 'x' * 129):
                with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                    local_mail.confirm_delivery('owner@example.test', self.report, run_id, self.since)
            with self.assertRaises(ValueError):
                local_mail.confirm_delivery('owner@example.test', self.report.with_name('missing.xlsx'), 'week-01', self.since)
            runner.assert_not_called()

    def test_process_error_never_exposes_com_output(self):
        windows = SimpleNamespace(name='nt', environ={'SystemRoot': 'C:/Windows'})
        failure = SimpleNamespace(returncode=1, stdout=b'private body', stderr=b'private token')
        with patch.object(local_mail, 'os', windows), patch.object(local_mail.subprocess, 'run', return_value=failure):
            with self.assertRaises(RuntimeError) as caught:
                local_mail.confirm_delivery('owner@example.test', self.report, 'week-01', self.since)
        self.assertNotIn('private', str(caught.exception))

    def test_powershell_parse_and_matching_with_fake_objects_only(self):
        executable = shutil.which('powershell') or shutil.which('pwsh')
        if not executable and os.name == 'nt':
            candidate = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            executable = str(candidate) if candidate.is_file() else None
        if not executable:
            self.skipTest('PowerShell parser is unavailable on this host')
        # The fixture parses the source and imports only four pure helper
        # definitions from its AST. Its top-level COM initialization never runs.
        # Run known synthetic test code with -Command; do not alter execution policy.
        source_arg = "'" + str(local_mail.SCRIPT).replace("'", "''") + "'"
        scratch_arg = "'" + self.directory.name.replace("'", "''") + "'"
        command = '& { ' + POWERSHELL_FIXTURE + ' } -Source ' + source_arg + ' -Scratch ' + scratch_arg
        completed = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-Command', command],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('Delivery helpers verified without Outlook', completed.stdout)


POWERSHELL_FIXTURE = r'''param([string]$Source, [string]$Scratch)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'PowerShell source did not parse' }
$helpers = $ast.FindAll({ param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
  $node.Name -in @('Get-CanonicalSmtp', 'Get-DeliverySender', 'Get-DeliveryFileHash', 'Test-DeliveryItem')
}, $true)
if ($helpers.Count -ne 4) { throw 'Expected delivery helper definitions' }
foreach ($helper in $helpers) { . ([scriptblock]::Create($helper.Extent.Text)) }
function Assert-Equal($Actual, $Expected) {
  if ($Actual -cne $Expected) { throw "Unexpected helper result: $Actual" }
}
$smtp = [pscustomobject]@{ Type='SMTP'; Address='Owner@Example.Test' }
Assert-Equal (Get-CanonicalSmtp $smtp) 'owner@example.test'
$exchange = [pscustomobject]@{ Type='EX'; Address='/o=Exchange/ou=Org/cn=Recipients/cn=owner' }
$exchange | Add-Member ScriptMethod GetExchangeUser { [pscustomobject]@{ PrimarySmtpAddress='Owner@Example.Test' } }
Assert-Equal (Get-CanonicalSmtp $exchange) 'owner@example.test'
$fallbackAccessor = [pscustomobject]@{}
$fallbackAccessor | Add-Member ScriptMethod GetProperty { param($tag) 'Owner@Example.Test' }
$fallback = [pscustomobject]@{ Type='EX'; Address='/o=Exchange/cn=owner'; PropertyAccessor=$fallbackAccessor }
$fallback | Add-Member ScriptMethod GetExchangeUser { throw 'Not an Exchange user' }
$fallback | Add-Member ScriptMethod GetExchangeDistributionList { throw 'Not a distribution list' }
Assert-Equal (Get-CanonicalSmtp $fallback) 'owner@example.test'
Assert-Equal (Get-CanonicalSmtp $null 'Owner Display Name') ''

$folderAccessor = [pscustomobject]@{}
$folderAccessor | Add-Member ScriptMethod LocalTimeToUTC { param($value) $value }
$folder = [pscustomobject]@{ StoreID='raw-private-store'; PropertyAccessor=$folderAccessor }
$recipient = [pscustomobject]@{ Type=1; Address='/o=Exchange/cn=owner'; AddressEntry=$exchange }
$recipients = [pscustomobject]@{ Count=1; Value=$recipient }
$recipients | Add-Member ScriptMethod Item { param($index) $this.Value }
$attached = [pscustomobject]@{ FileName='weekly.xlsx'; Size=6; Content='report' }
$attached | Add-Member ScriptMethod SaveAsFile { param($path) [IO.File]::WriteAllBytes($path, [Text.Encoding]::UTF8.GetBytes($this.Content)) }
$attachments = [pscustomobject]@{ Count=1; Value=$attached }
$attachments | Add-Member ScriptMethod Item { param($index) $this.Value }
$start = [datetime]::Parse('2026-10-02T00:00:00Z').ToUniversalTime()
$end = $start.AddHours(1)
$item = [pscustomobject]@{ Class=43; Subject='[ARIADNE] 주간업무보고 week-01';
  ReceivedTime=$start.AddMinutes(1); Sender=$exchange; SenderEmailAddress='/o=Exchange/cn=owner';
  Recipients=$recipients; Attachments=$attachments; EntryID='raw-private-entry' }
$sha = [Security.Cryptography.SHA256]::Create()
try { $expectedHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes('report')))).Replace('-','').ToLowerInvariant() }
finally { $sha.Dispose() }
function Check-Item {
  Test-DeliveryItem $item $folder 'owner@example.test' '[ARIADNE] 주간업무보고 week-01' 'weekly.xlsx' $expectedHash 6 $start $end $Scratch
}
$receipt = Check-Item
Assert-Equal $receipt.state 'matched'
if ($receipt.id -cnotmatch '^[0-9a-f]{64}$') { throw 'Receipt ID was not sanitized' }
if ($receipt.receivedAt -cnotmatch 'Z$') { throw 'Receipt date was not UTC' }
if (@(Get-ChildItem -LiteralPath $Scratch -Filter '*.attachment').Count -ne 0) { throw 'Attachment scratch file remained' }
$attached.Content = 'wrong!'
Assert-Equal (Check-Item).state 'mismatch'
$attached.Content = 'report'
$attached.FileName = 'other.xlsx'
Assert-Equal (Check-Item).state 'mismatch'
$attached.FileName = 'weekly.xlsx'
$item.Subject = '[ARIADNE] 주간업무보고 week-010'
Assert-Equal (Check-Item).state 'skip'
$item.Subject = '[ARIADNE] 주간업무보고 week-01'
$item.ReceivedTime = $start.AddSeconds(-1)
Assert-Equal (Check-Item).state 'skip'
$item.ReceivedTime = $start.AddMinutes(1)
$item.Sender = [pscustomobject]@{ Type='SMTP'; Address='other@example.test' }
Assert-Equal (Check-Item).state 'skip'
$item.Sender = $exchange
$recipient.Type = 2
Assert-Equal (Check-Item).state 'skip'
$recipient.Type = 1
$recipient.AddressEntry = [pscustomobject]@{ Type='SMTP'; Address='other@example.test' }
Assert-Equal (Check-Item).state 'skip'
$recipient.AddressEntry = $exchange
$recipients.Count = 2
Assert-Equal (Check-Item).state 'skip'
Write-Output 'Delivery helpers verified without Outlook'
'''


if __name__ == '__main__':
    unittest.main()
