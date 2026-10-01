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
$backgroundPython = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $backgroundPython)) {
  throw 'pythonw.exe is required so scheduled work does not open or focus a console window'
}
$action = New-ScheduledTaskAction -Execute $backgroundPython -Argument $arguments -WorkingDirectory $Project
if ([TimeZoneInfo]::Local.BaseUtcOffset -ne [TimeSpan]::FromHours(9)) {
  throw 'Set this PC timezone to Korea Standard Time before registering local schedules'
}
$triggers = @(9..18 | ForEach-Object {
  New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At (Get-Date).Date.AddHours($_)
})
$triggers += New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At (Get-Date).Date.AddHours(16).AddMinutes(15)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$options = New-ScheduledTaskSettingsSet -WakeToRun -MultipleInstances Queue -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
# No delayed catch-up trigger: Teams is additionally guarded to noon + five minutes
# and claimed once per weekday by the durable DB before any screen navigation.
Register-ScheduledTask -TaskName 'ARIADNE Weekly Report' -Action $action -Trigger $triggers -Principal $principal -Settings $options -Description '평일 09~18시 메일·폴더·결재 수집. Teams는 평일 12시에 1회, 사용 중이면 생략. 금요일 16시 원본 보관, 16시15분 보고 생성. 이전 작업 종료 후 순차 실행.' -Force
