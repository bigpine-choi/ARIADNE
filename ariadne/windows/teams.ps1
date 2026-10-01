param([string]$SettingsPath,[string]$ResultPath)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$config=Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$chats=@($config.chats)
if ($chats.Count -eq 0) { throw 'Explicit chat scope is required' }
$roots=[System.Windows.Automation.AutomationElement]::RootElement.FindAll(
  [System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
$windows=@($roots | Where-Object {$_.Current.Name -match '^채팅 \| .+ \| Microsoft Teams$'})
if ($windows.Count -ne 1) { throw 'Exactly one logged-in Teams window is required' }
$window=$windows[0]
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AriadneForeground {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr handle,out uint id);
}
'@
function Foreground-Teams {
  [uint32]$pidValue=0
  $null=[AriadneForeground]::GetWindowThreadProcessId([AriadneForeground]::GetForegroundWindow(),[ref]$pidValue)
  return $pidValue -eq $window.Current.ProcessId
}
if (Foreground-Teams) {
  $skip=@{messages=@();warnings=@('teams_in_use_collection_deferred');coverage=@()}
  [IO.File]::WriteAllText($ResultPath,(ConvertTo-Json $skip -Depth 5 -Compress),(New-Object Text.UTF8Encoding($false)))
  exit 0
}
$original=$window.Current.Name.Split('|')[1].Trim()
$messages=New-Object System.Collections.Generic.List[object]
$warnings=New-Object System.Collections.Generic.List[string]
$coverage=New-Object System.Collections.Generic.List[object]
function Elements { return $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition) }
function Select-Chat([string]$chat) {
  if (Foreground-Teams) {throw 'User is using Teams; collection deferred'}
  $nodes=@((Elements) | Where-Object {
    $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::TreeItem -and
    ($_.Current.Name -eq ('그룹 채팅 '+$chat) -or $_.Current.Name -eq ('그룹 채팅 '+$chat+' 음소거됨'))
  })
  if ($nodes.Count -ne 1) { throw 'Configured chat is missing or ambiguous' }
  $pattern=$nodes[0].GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
  $pattern.Select()
  for ($attempt=0;$attempt -lt 25;$attempt++) {
    Start-Sleep -Milliseconds 200
    if ($window.Current.Name.Split('|')[1].Trim() -eq $chat) { return }
  }
  throw 'Chat selection did not match the configured scope'
}
try {
  foreach ($chat in $chats) {
    try {
      Select-Chat $chat
      $seen=@{}
      $reached=$false
      $scroll=$null
      $initialPercent=-1
      for ($step=0;$step -lt 80;$step++) {
        if (Foreground-Teams) {throw 'User is using Teams; collection stopped'}
        if ($window.Current.Name.Split('|')[1].Trim() -ne $chat) { throw 'Chat changed during collection' }
        $nodes=Elements
        $bodies=@($nodes | Where-Object {$_.Current.AutomationId -match '^message-body-\d+$'})
        if ($bodies.Count -eq 0) { throw 'Message layout unavailable' }
        if ($step -eq 0) {
          $parent=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($bodies[0])
          for ($depth=0;$depth -lt 12 -and $null -ne $parent;$depth++) {
            $candidate=$null
            if ($parent.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern,[ref]$candidate) -and $candidate.Current.VerticallyScrollable) {
              $scroll=$candidate; $initialPercent=$scroll.Current.VerticalScrollPercent
              $scroll.SetScrollPercent(-1,100)
              Start-Sleep -Milliseconds 300
              break
            }
            $parent=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
          }
          if ($null -ne $scroll) { continue }
        }
        $oldest=$null
        foreach ($body in $bodies) {
          $id=$body.Current.AutomationId
          $label=$body.Current.Name
          if (-not $seen.ContainsKey($id)) {
            $seen[$id]=$true
            $messages.Add(@{id=$id;text=$label;chat=$chat})
          }
          if ($label -match '(\d{4})년 (\d{1,2})월 (\d{1,2})일 .+ (오전|오후) (\d{1,2}):(\d{2})') {
            $hour=[int]$Matches[5]
            if ($Matches[4] -eq '오후' -and $hour -lt 12) {$hour+=12}
            if ($Matches[4] -eq '오전' -and $hour -eq 12) {$hour=0}
            $instant=New-Object DateTimeOffset([int]$Matches[1],[int]$Matches[2],[int]$Matches[3],$hour,[int]$Matches[6],0,[TimeSpan]::FromHours(9))
            if ($null -eq $oldest -or $instant -lt $oldest) {$oldest=$instant}
          }
        }
        if ($null -ne $oldest -and $oldest -lt [DateTimeOffset]::Parse($config.start)) {$reached=$true;break}
        if ($null -eq $scroll) {break}
        if ($scroll.Current.VerticalScrollPercent -le 0) {$reached=$true;break}
        $before=$scroll.Current.VerticalScrollPercent
        $scroll.Scroll([System.Windows.Automation.ScrollAmount]::NoAmount,[System.Windows.Automation.ScrollAmount]::LargeDecrement)
        Start-Sleep -Milliseconds 350
        if ($scroll.Current.VerticalScrollPercent -eq $before) {break}
      }
      if ($null -ne $scroll -and $initialPercent -ge 0 -and -not (Foreground-Teams)) {
        try {$scroll.SetScrollPercent(-1,$initialPercent)} catch {}
      }
      $coverage.Add(@{chat=$chat;messages=$seen.Count;reachedStart=$reached})
      if (-not $reached) {$warnings.Add('teams_history_start_not_reached')}
    } catch {
      if (Foreground-Teams) {
        $warnings.Add('teams_in_use_collection_deferred')
        break
      }
      $warnings.Add('teams_chat_collection_failed')
      $coverage.Add(@{chat=$chat;messages=0;reachedStart=$false})
    }
  }
} finally {
  if (-not (Foreground-Teams)) {try {Select-Chat $original} catch {}}
}
$result=@{messages=@($messages.ToArray());warnings=@($warnings.ToArray());coverage=@($coverage.ToArray())}
[IO.File]::WriteAllText($ResultPath,(ConvertTo-Json -InputObject $result -Depth 7 -Compress),(New-Object Text.UTF8Encoding($false)))
