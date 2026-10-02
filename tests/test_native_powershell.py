"""Synthetic preflight/transport tests: no Outlook, Teams, or real mail."""
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ariadne import automation_cli, local_mail, native_powershell as native, teams_uia
from ariadne.local_settings import DEFAULTS


def policy(**overrides):
    scopes = {scope:'Undefined' for scope in native.SCOPES}
    scopes.update(overrides)
    effective = next((scopes[scope] for scope in native.SCOPES if scopes[scope] != 'Undefined'), 'Restricted')
    return {'version':'5.1.19041.1','effectivePolicy':effective,'scopes':scopes}


class NativePreflightTests(unittest.TestCase):
    def setUp(self):
        self.windows = SimpleNamespace(name='nt',environ={'SystemRoot':'C:/Windows'})
        self.executable = Path('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
        for fixture in (patch.object(native,'os',self.windows),
                        patch.object(native,'powershell_executable',return_value=self.executable)):
            fixture.start()
            self.addCleanup(fixture.stop)

    def diagnose(self, value, registered=True):
        with patch.object(native,'inspect_policy',return_value=value), \
                patch.object(native,'classic_outlook_registered',return_value=registered), \
                patch.object(native,'_remote_mark',return_value=False):
            return native.diagnostics(local_mail.SCRIPT,require_outlook=True)

    def test_inspects_actual_windows_powershell_without_running_scripts(self):
        completed = SimpleNamespace(returncode=0,stdout=json.dumps(policy()),stderr='')
        with patch.object(native.subprocess,'run',return_value=completed) as runner:
            self.assertEqual(policy(),native.inspect_policy())
        args, kwargs = runner.call_args
        command = args[0]
        self.assertEqual(str(self.executable),command[0])
        self.assertIn('-Command',command)
        self.assertIn('Get-ExecutionPolicy -List',command[-1])
        self.assertNotIn('-File',command)
        self.assertNotIn('-ExecutionPolicy',command)
        self.assertTrue(kwargs['capture_output'])
        self.assertEqual(20,kwargs['timeout'])

    def test_powershell_7_module_paths_are_removed_from_child_only(self):
        inherited = {'SystemRoot':'C:/Windows','PSMODULEPATH':'PowerShell7/modules',
                     'PSExecutionPolicyPreference':'AllSigned','OTHER':'preserved'}
        with patch.object(native,'os',SimpleNamespace(name='nt',environ=inherited)):
            child = native.child_environment()
        self.assertNotIn('PSMODULEPATH',child)
        self.assertEqual('AllSigned',child['PSExecutionPolicyPreference'])
        self.assertEqual('preserved',child['OTHER'])
        self.assertEqual('PowerShell7/modules',inherited['PSMODULEPATH'])

    def test_malformed_unknown_or_inconsistent_policy_fails_closed_and_redacted(self):
        malformed = [policy(),policy(),policy(),{'secret':'private token'},policy()]
        malformed[0]['scopes']['MachinePolicy']='Unknown'
        malformed[1]['version']='7.5.0'
        malformed[2]['scopes']['MachinePolicy']='Restricted'
        malformed[2]['effectivePolicy']='RemoteSigned'
        malformed[4]['effectivePolicy']='Undefined'
        for value in malformed:
            with self.subTest(value=value), patch.object(native.subprocess,'run',return_value=
                    SimpleNamespace(returncode=0,stdout=json.dumps(value),stderr='private token')):
                with self.assertRaisesRegex(native.NativeLaunchError,'POWERSHELL_DIAGNOSTICS_UNAVAILABLE') as caught:
                    native.inspect_policy()
                self.assertNotIn('private',str(caught.exception))

    def test_missing_classic_outlook_blocks_before_any_collector_or_mail_process(self):
        start = datetime.now(timezone.utc)-timedelta(days=1)
        with patch.object(local_mail,'os',self.windows), \
                patch.object(native,'inspect_policy',return_value=policy()), \
                patch.object(native,'classic_outlook_registered',return_value=False), \
                patch.object(native,'_remote_mark',return_value=False), \
                patch.object(local_mail.subprocess,'run') as runner:
            with self.assertRaisesRegex(native.NativeLaunchError,'CLASSIC_OUTLOOK_MISSING'):
                local_mail.collect('owner@example.test',start,start+timedelta(days=1))
            runner.assert_not_called()

    def test_ordinary_restricted_is_only_a_child_process_remotesigned_request(self):
        status = self.diagnose(policy(LocalMachine='Restricted'))
        self.assertTrue(status['ready'])
        self.assertEqual('Restricted',status['effectivePolicy'])
        self.assertEqual('RemoteSigned',status['processPolicyRequested'])
        self.assertFalse(status['persistentPolicyChanged'])
        self.assertFalse(status['accountVerified'])
        with patch.object(native,'diagnostics',return_value=status):
            command = native.script_command(local_mail.SCRIPT,['-Mailbox',"owner'quoted@example.test"],require_outlook=True)
        self.assertIsInstance(command,list)
        self.assertEqual(str(self.executable),command[0])
        self.assertEqual('RemoteSigned',command[command.index('-ExecutionPolicy')+1])
        self.assertEqual("owner'quoted@example.test",command[-1])
        for flag in ('-NoProfile','-NonInteractive','-STA','-File'):
            self.assertIn(flag,command)
        self.assertNotIn('Bypass',command)
        self.assertNotIn('Set-ExecutionPolicy',' '.join(command))

    def test_effective_group_policy_restricted_blocks_and_does_not_override_it(self):
        for value in (policy(MachinePolicy='Restricted'),policy(UserPolicy='Restricted')):
            with self.subTest(value=value):
                status = self.diagnose(value)
                self.assertEqual(['POWERSHELL_POLICY_BLOCKED'],status['codes'])
                self.assertFalse(status['ready'])
                self.assertIsNone(status['processPolicyRequested'])
                with patch.object(native,'diagnostics',return_value=status):
                    with self.assertRaisesRegex(native.NativeLaunchError,'POWERSHELL_POLICY_BLOCKED'):
                        native.script_command(local_mail.SCRIPT,[])

    def test_higher_group_policy_precedence_is_respected(self):
        status = self.diagnose(policy(MachinePolicy='RemoteSigned',UserPolicy='Restricted'))
        self.assertTrue(status['ready'])
        self.assertEqual('RemoteSigned',status['groupPolicy'])
        with patch.object(native,'diagnostics',return_value=status):
            command = native.script_command(local_mail.SCRIPT,[])
        self.assertNotIn('-ExecutionPolicy',command)

    def test_allsigned_requirement_is_not_relaxed(self):
        for value in (policy(UserPolicy='AllSigned'),policy(Process='AllSigned'),policy(CurrentUser='AllSigned')):
            with self.subTest(value=value):
                self.assertEqual(['POWERSHELL_POLICY_BLOCKED'],self.diagnose(value)['codes'])

    def test_download_mark_is_preserved_and_blocks_unsigned_scripts(self):
        with patch.object(native,'inspect_policy',return_value=policy(LocalMachine='RemoteSigned')), \
                patch.object(native,'_remote_mark',return_value=True), \
                patch.object(native.subprocess,'run') as runner:
            status = native.diagnostics(local_mail.SCRIPT)
        self.assertEqual(['POWERSHELL_POLICY_BLOCKED'],status['codes'])
        runner.assert_not_called()

    def test_missing_local_mark_is_allowed_but_invalid_encoding_and_zone_are_not(self):
        for error, expected in ((FileNotFoundError(),False),
                (UnicodeDecodeError('utf-8',b'\xff',0,1,'invalid'),True),
                (PermissionError(),True)):
            with self.subTest(error=type(error).__name__),patch.object(Path,'read_text',side_effect=error):
                self.assertIs(native._remote_mark(local_mail.SCRIPT),expected)
        for mark, expected in (('[ZoneTransfer]\nZoneId=2\n',False),
                ('[ZoneTransfer]\nZoneId=3\n',True),('not a zone',True),
                ('ZoneId=1\nZoneId=2\n',True),('ZoneId=99\n',True)):
            with self.subTest(mark=mark),patch.object(Path,'read_text',return_value=mark):
                self.assertIs(native._remote_mark(local_mail.SCRIPT),expected)

    def test_doctor_reports_both_independent_blockers_without_account_access(self):
        status = self.diagnose(policy(UserPolicy='Restricted'),registered=False)
        self.assertEqual(['POWERSHELL_POLICY_BLOCKED','CLASSIC_OUTLOOK_MISSING'],status['codes'])
        self.assertFalse(status['accountVerified'])
        rendered = json.dumps(status)
        self.assertNotIn('C:/',rendered)
        self.assertNotIn('owner@',rendered)

    def test_cli_doctor_keeps_settings_and_native_readiness_separate(self):
        mail_status = self.diagnose(policy(),registered=False)
        teams_status = self.diagnose(policy())
        output = StringIO()
        with patch.object(automation_cli,'load_settings',return_value={**DEFAULTS,'mailbox':'owner@example.test'}), \
                patch('ariadne.credentials.CredentialStore') as credentials, \
                patch.object(native,'diagnostics',side_effect=[mail_status,teams_status]), \
                patch.object(local_mail,'collect') as mail,patch.object(teams_uia,'collect') as teams, \
                patch.object(sys,'argv',['ariadne-automation','doctor']),redirect_stdout(output):
            credentials.return_value.status.return_value = {'saved':False,'environmentConfigured':False}
            automation_cli.main()
        result = json.loads(output.getvalue())
        self.assertTrue(result['mailboxConfigured'])
        self.assertFalse(result['mailDiagnostics']['ready'])
        self.assertEqual(['CLASSIC_OUTLOOK_MISSING'],result['mailDiagnostics']['codes'])
        self.assertFalse(result['apiConnectionVerified'])
        self.assertFalse(result['mailDiagnostics']['accountVerified'])
        mail.assert_not_called()
        teams.assert_not_called()

    def test_unreadable_registration_is_not_reported_as_missing(self):
        status = self.diagnose(policy(),registered=None)
        self.assertEqual(['CLASSIC_OUTLOOK_UNVERIFIED'],status['codes'])

    def test_arbitrary_script_is_rejected_before_policy_or_process(self):
        with tempfile.TemporaryDirectory() as directory:
            rogue = Path(directory)/'outlook.ps1'
            rogue.write_text('do not execute',encoding='utf-8')
            with patch.object(native,'inspect_policy') as inspector:
                with self.assertRaisesRegex(native.NativeLaunchError,'POWERSHELL_SCRIPT_INVALID'):
                    native.script_command(rogue,[])
                inspector.assert_not_called()

    def test_registry_missing_and_access_denied_are_distinguished_without_com(self):
        fake = SimpleNamespace(KEY_WOW64_64KEY=1,KEY_WOW64_32KEY=2,KEY_READ=4,HKEY_CLASSES_ROOT=5)
        for error, expected in ((FileNotFoundError(),False),(PermissionError(),None)):
            with self.subTest(error=type(error).__name__),patch.dict(sys.modules,{'winreg':fake}), \
                    patch.object(fake,'OpenKey',create=True,side_effect=error):
                self.assertIs(native.classic_outlook_registered(),expected)


class NativeTransportErrorsTests(unittest.TestCase):
    def setUp(self):
        self.windows = SimpleNamespace(name='nt',environ={'SystemRoot':'C:/Windows'})
        self.start = datetime.now(timezone.utc)-timedelta(days=1)
        self.end = self.start+timedelta(days=1)
        for module in (local_mail, teams_uia):
            fixture = patch.object(module,'script_command',return_value=['synthetic-never-executed'])
            fixture.start()
            self.addCleanup(fixture.stop)

    def test_fixed_missing_profile_marker_is_redacted(self):
        failure = SimpleNamespace(returncode=1,stdout=b'private mail body',
            stderr=b'Explicit mailbox not found; private token C:/private/path')
        with patch.object(local_mail,'os',self.windows),patch.object(local_mail.subprocess,'run',return_value=failure):
            with self.assertRaisesRegex(native.NativeLaunchError,'MAIL_PROFILE_UNAVAILABLE') as caught:
                local_mail.collect('owner@example.test',self.start,self.end)
        self.assertNotIn('private',str(caught.exception))
        self.assertNotIn('owner@',str(caught.exception))

    def test_engine_policy_error_is_redacted(self):
        failure = SimpleNamespace(returncode=1,stdout=b'secret',stderr=b'PSSecurityException secret')
        with patch.object(local_mail,'os',self.windows),patch.object(local_mail.subprocess,'run',return_value=failure):
            with self.assertRaisesRegex(native.NativeLaunchError,'POWERSHELL_POLICY_BLOCKED') as caught:
                local_mail.collect('owner@example.test',self.start,self.end)
        self.assertNotIn('secret',str(caught.exception))

    def test_timeout_never_exposes_collector_parameters_or_output(self):
        timeout = subprocess.TimeoutExpired(['private mailbox'],180,output=b'private body',stderr=b'private token')
        with patch.object(local_mail,'os',self.windows),patch.object(local_mail.subprocess,'run',side_effect=timeout):
            with self.assertRaisesRegex(native.NativeLaunchError,'NATIVE_COLLECTION_TIMEOUT') as caught:
                local_mail.collect('owner@example.test',self.start,self.end)
        self.assertNotIn('private',str(caught.exception))

    def test_teams_uses_same_guard_and_never_exposes_raw_errors(self):
        failure = SimpleNamespace(returncode=1,stdout=b'private chat',stderr=b'private token')
        with patch.object(teams_uia,'os',self.windows),patch.object(teams_uia.subprocess,'run',return_value=failure):
            with self.assertRaisesRegex(native.NativeLaunchError,'TEAMS_COLLECTION_UNAVAILABLE') as caught:
                teams_uia.collect(['Synthetic Chat'],self.start,self.end)
        self.assertNotIn('private',str(caught.exception))


if __name__ == '__main__':
    unittest.main()
