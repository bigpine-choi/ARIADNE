param(
  [ValidateSet('collect','send','probe')][string]$Mode,
  [string]$Mailbox,[string]$ResultPath,[string]$StartUtc,[string]$EndUtc,
  [string]$Attachment,[string]$RunId
)
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject Outlook.Application
$session = $app.GetNamespace('MAPI')
$account = @($session.Accounts | Where-Object { $_.SmtpAddress -ieq $Mailbox })
if ($account.Count -ne 1) { throw 'Explicit mailbox not found' }
$account = $account[0]
if ($Mode -eq 'probe') {
  $result = @{ connected=$true; accountType=[int]$account.AccountType }
} elseif ($Mode -eq 'send') {
  if (-not (Test-Path -LiteralPath $Attachment -PathType Leaf)) { throw 'Report missing' }
  # Both account and sole recipient are pinned to the configured owner.
  $mail = $app.CreateItem(0)
  $mail.SendUsingAccount = $account
  $mail.To = $Mailbox
  $mail.Subject = "[ARIADNE] 주간업무보고 $RunId"
  $mail.Body = '이번 주 업무보고입니다. 수집 범위와 확인 필요 사항은 첨부 파일에 표시되어 있습니다.'
  $null = $mail.Attachments.Add($Attachment)
  $mail.Send()
  $result = @{ submitted=$true; delivered=$false }
} else {
  $start = [DateTimeOffset]::Parse($StartUtc).UtcDateTime
  $end = [DateTimeOffset]::Parse($EndUtc).UtcDateTime
  if ($start -ge $end) { throw 'Invalid window' }
  $messages = New-Object System.Collections.Generic.List[object]
  $warnings = New-Object System.Collections.Generic.List[string]
  function Read-Folder($folder,$direction) {
    # DASL datetime literals use UTC; a second exact comparison handles boundaries.
    $prop = if ($direction -eq 'sent') { '0x00390040' } else { '0x0E060040' }
    $uri = 'http://schemas.microsoft.com/mapi/proptag/' + $prop
    $filter = '@SQL="' + $uri + '" >= ' + "'" + $start.ToString('yyyy-MM-dd HH:mm:ss') + "' AND " + '"' + $uri + '" < ' + "'" + $end.ToString('yyyy-MM-dd HH:mm:ss') + "'"
    $items = $folder.Items.Restrict($filter)
    foreach ($item in $items) {
      if ($item.Class -ne 43) { continue }
      try {
        $timestamp = if ($direction -eq 'sent') { $item.SentOn } else { $item.ReceivedTime }
        $utc = $folder.PropertyAccessor.LocalTimeToUTC($timestamp)
        if ($utc -lt $start -or $utc -ge $end) { continue }
        $sender = $item.SenderEmailAddress
        if ($item.SenderEmailType -eq 'EX' -and $null -ne $item.Sender) {
          $exchange = $item.Sender.GetExchangeUser()
          if ($null -ne $exchange) { $sender = $exchange.PrimarySmtpAddress }
        }
        $body = [string]$item.Body
        if ($body.Length -gt 100000) { $body=$body.Substring(0,100000); $warnings.Add('mail_body_truncated') }
        $internetId = ''
        try { $internetId=$item.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x1035001F') } catch { }
        $messages.Add(@{ source_id=($folder.StoreID + ':' + $item.EntryID);
          provider='outlook_com'; direction=$direction; timestamp=$utc.ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'");
          subject=[string]$item.Subject; body=$body; sender=[string]$sender;
          conversation=[string]$item.ConversationID; internet_message_id=[string]$internetId;
          attachment_count=[int]$item.Attachments.Count })
      } catch { $warnings.Add('mail_item_unreadable') }
    }
    foreach ($child in $folder.Folders) { Read-Folder $child $direction }
  }
  $store = $account.DeliveryStore
  Read-Folder ($store.GetDefaultFolder(6)) 'received'
  Read-Folder ($store.GetDefaultFolder(5)) 'sent'
  $result = @{ messages=@($messages.ToArray()); warnings=@($warnings.ToArray());
    complete=($warnings.Count -eq 0);
    scope='Configured account Inbox and Sent Items including subfolders; locally available Outlook items only' }
}
$json = ConvertTo-Json -InputObject $result -Depth 8 -Compress
[IO.File]::WriteAllText($ResultPath,$json,(New-Object Text.UTF8Encoding($false)))

