"""Preflight the actual Windows PowerShell host; never change stored OS policy."""
import json
import os
from pathlib import Path
import re
import subprocess


SCOPES = ('MachinePolicy', 'UserPolicy', 'Process', 'CurrentUser', 'LocalMachine')
POLICIES = {'Undefined', 'Restricted', 'AllSigned', 'RemoteSigned', 'Unrestricted', 'Bypass'}
SCRIPT_DIRECTORY = Path(__file__).parent / 'windows'
MESSAGES = {
    'WINDOWS_REQUIRED': 'Windows에서 실행하세요.',
    'POWERSHELL_UNAVAILABLE': 'Windows PowerShell 5.1을 확인하세요.',
    'POWERSHELL_DIAGNOSTICS_UNAVAILABLE': 'Windows PowerShell 정책을 확인하지 못했습니다.',
    'POWERSHELL_POLICY_BLOCKED': 'Windows PowerShell 정책 또는 파일 신뢰 확인이 필요합니다.',
    'POWERSHELL_SCRIPT_INVALID': '설치된 ARIADNE 수집 스크립트를 확인하세요.',
    'CLASSIC_OUTLOOK_MISSING': '클래식 Outlook 설치를 확인하세요.',
    'CLASSIC_OUTLOOK_UNVERIFIED': '클래식 Outlook 등록 상태를 확인하지 못했습니다.',
    'MAIL_PROFILE_UNAVAILABLE': '클래식 Outlook의 본인 회사 메일 프로필을 확인하세요.',
    'OUTLOOK_COM_UNAVAILABLE': '클래식 Outlook 로그인과 프로그램 접근 정책을 확인하세요.',
    'TEAMS_COLLECTION_UNAVAILABLE': 'Teams 로그인과 접근성 수집 범위를 확인하세요.',
    'NATIVE_COLLECTION_TIMEOUT': '수집 시간이 초과되었습니다. 현재 실행 상태를 확인하세요.',
}


class NativeLaunchError(RuntimeError):
    """Only a fixed code/message reaches the UI; COM output stays private."""
    def __init__(self, code):
        self.code = code if code in MESSAGES else 'POWERSHELL_DIAGNOSTICS_UNAVAILABLE'
        super().__init__(self.code + ': ' + MESSAGES[self.code])


def powershell_executable():
    root = os.environ.get('SystemRoot', '')
    executable = Path(root) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not root or not executable.is_file():
        raise NativeLaunchError('POWERSHELL_UNAVAILABLE')
    return executable


def child_environment():
    # A Python child of pwsh 7 otherwise inherits its incompatible module paths.
    # Let Windows PowerShell rebuild its own defaults in this child only.
    return {name:value for name,value in os.environ.items()
            if name.casefold() != 'psmodulepath'}


def inspect_policy():
    """-Command reads policy without loading a .ps1, Outlook, or Teams."""
    if os.name != 'nt':
        raise NativeLaunchError('WINDOWS_REQUIRED')
    executable = powershell_executable()
    command = (
        "$ErrorActionPreference='Stop'; $policies=@{}; "
        "foreach($item in (Get-ExecutionPolicy -List)) { "
        "$policies[[string]$item.Scope]=[string]$item.ExecutionPolicy }; "
        "@{version=$PSVersionTable.PSVersion.ToString(); "
        "effectivePolicy=[string](Get-ExecutionPolicy); scopes=$policies} "
        "| ConvertTo-Json -Compress -Depth 3"
    )
    try:
        completed = subprocess.run(
            [str(executable), '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=child_environment(),
        )
        if completed.returncode or len(completed.stdout) > 16384:
            raise ValueError
        value = json.loads(completed.stdout.lstrip('\ufeff'))
        if (not isinstance(value, dict) or set(value) != {'version', 'effectivePolicy', 'scopes'}
                or not isinstance(value['version'], str)
                or not re.fullmatch(r'5\.1\.\d+\.\d+', value['version'])
                or value['effectivePolicy'] not in POLICIES - {'Undefined'}
                or not isinstance(value['scopes'], dict) or set(value['scopes']) != set(SCOPES)
                or any(value['scopes'][scope] not in POLICIES for scope in SCOPES)):
            raise ValueError
        declared = next((value['scopes'][scope] for scope in SCOPES
                         if value['scopes'][scope] != 'Undefined'), None)
        if declared is not None and declared != value['effectivePolicy']:
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        raise NativeLaunchError('POWERSHELL_DIAGNOSTICS_UNAVAILABLE') from None


def classic_outlook_registered():
    """Registry/file inspection only; registration does not verify a login."""
    import winreg
    unreadable = False
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            access = winreg.KEY_READ | view
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'Outlook.Application\CLSID', 0, access) as key:
                clsid, _ = winreg.QueryValueEx(key, '')
            if not isinstance(clsid, str) or not re.fullmatch(r'\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}', clsid):
                continue
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, 'CLSID\\' + clsid + r'\LocalServer32', 0, access) as key:
                registered, _ = winreg.QueryValueEx(key, '')
            if not isinstance(registered, str):
                continue
            match = re.match(r'^\s*(?:"([^"]+\.exe)"|(.+?\.exe))(?:\s|$)', registered, re.I)
            if match and Path(os.path.expandvars(match.group(1) or match.group(2))).is_file():
                return True
        except FileNotFoundError:
            continue
        except OSError:
            unreadable = True
    return None if unreadable else False


def _bundled_script(script):
    script = Path(script).resolve()
    if (script.parent != SCRIPT_DIRECTORY.resolve()
            or script.name not in {'outlook.ps1', 'teams.ps1'} or not script.is_file()):
        raise NativeLaunchError('POWERSHELL_SCRIPT_INVALID')
    return script


def _remote_mark(script):
    """Do not remove download marks or relax RemoteSigned for a remote file."""
    try:
        mark = Path(str(script) + ':Zone.Identifier').read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        return True  # File trust could not be verified.
    zones = re.findall(r'^ZoneId\s*=\s*(\d+)\s*$', mark, re.M | re.I)
    return len(zones) != 1 or zones[0] not in {'0', '1', '2'}


def diagnostics(script, *, require_outlook=False):
    codes = []
    result = {'ready': False, 'host': 'windows_powershell_5_1',
              'persistentPolicyChanged': False, 'accountVerified': False}
    try:
        script = _bundled_script(script)
        policy = inspect_policy()
        result.update(policy)
        group = next((policy['scopes'][scope] for scope in SCOPES[:2]
                      if policy['scopes'][scope] != 'Undefined'), None)
        result['groupPolicy'] = group or 'Undefined'
        # A Process flag cannot override GroupPolicy. AllSigned requires signed
        # distribution; these reviewed source scripts are not signed packages.
        if (group in {'Restricted', 'AllSigned'}
                or policy['effectivePolicy'] == 'AllSigned' or _remote_mark(script)):
            codes.append('POWERSHELL_POLICY_BLOCKED')
        result['processPolicyRequested'] = None if group else 'RemoteSigned'
    except NativeLaunchError as error:
        codes.append(error.code)
    if require_outlook and os.name == 'nt':
        registered = classic_outlook_registered()
        result['classicOutlookRegistered'] = registered
        if registered is not True:
            codes.append('CLASSIC_OUTLOOK_MISSING' if registered is False else 'CLASSIC_OUTLOOK_UNVERIFIED')
    result['codes'] = list(dict.fromkeys(codes))
    result['ready'] = not codes
    return result


def require_ready(script, *, require_outlook=False):
    status = diagnostics(script, require_outlook=require_outlook)
    if not status['ready']:
        raise NativeLaunchError(status['codes'][0])
    return status


def script_command(script, arguments, *, require_outlook=False):
    script = _bundled_script(script)
    status = require_ready(script, require_outlook=require_outlook)
    command = [str(powershell_executable()), '-NoProfile', '-NonInteractive', '-STA']
    if status['processPolicyRequested']:
        command += ['-ExecutionPolicy', 'RemoteSigned']
    return command + ['-File', str(script)] + list(arguments)


def failure_code(completed, *, outlook=False):
    """Classify fixed engine markers internally; never return output or paths."""
    raw = getattr(completed, 'stderr', b'')
    error = raw if isinstance(raw, bytes) else str(raw).encode('utf-8')
    if b'PSSecurityException' in error:
        return 'POWERSHELL_POLICY_BLOCKED'
    if outlook and b'Explicit mailbox not found' in error:
        return 'MAIL_PROFILE_UNAVAILABLE'
    return 'OUTLOOK_COM_UNAVAILABLE' if outlook else 'TEAMS_COLLECTION_UNAVAILABLE'
