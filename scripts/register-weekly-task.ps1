param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Project,
      [Parameter(Mandatory=$true)][string]$Settings,
      [Parameter(Mandatory=$true)][string]$DataDirectory)
$ErrorActionPreference = 'Stop'
foreach ($p in @($Python,$Project,$Settings)) {
  if (-not (Test-Path -LiteralPath $p)) { throw 'Required local path is missing' }
  if ($p.Contains('"')) { throw 'Invalid task path' }
}
if ($DataDirectory.Contains('"')) { throw 'Invalid data directory' }
# Logged-in owner only. No stored Windows password, SYSTEM task or elevation.
$arguments = '-m ariadne.automation_cli --settings "' + $Settings + '" --data-dir "' + $DataDirectory + '" tick'
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddDays(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$options = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName 'ARIADNE Weekly Report' -Action $action -Trigger $trigger -Principal $principal -Settings $options -Description '금요일 16시 한국시간 보고서 생성. 절전 후 복구, 본인 계정으로 실행.'
