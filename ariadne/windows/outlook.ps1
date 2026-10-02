param(
  [ValidateSet('collect','send','probe','confirm')][string]$Mode,
  [string]$Mailbox,[string]$ResultPath,[string]$StartUtc,[string]$EndUtc,
  [string]$Attachment,[string]$RunId,[string]$AttachmentSha256
)
$ErrorActionPreference = 'Stop'

function Get-CanonicalSmtp($Entry, [string]$Fallback = '') {
  # Exchange addresses can be legacy distinguished names rather than SMTP.
  $value = ''
  if ($null -ne $Entry) {
    if ($Entry.Type -eq 'EX') {
      try { $value = [string]$Entry.GetExchangeUser().PrimarySmtpAddress } catch { }
      if (-not $value) {
        try { $value = [string]$Entry.GetExchangeDistributionList().PrimarySmtpAddress } catch { }
      }
    } else { $value = [string]$Entry.Address }
    if (-not $value -or $value -notmatch '@') {
      foreach ($tag in @('0x39FE001F','0x39FE001E')) {
        try { $value = [string]$Entry.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/' + $tag) } catch { }
        if ($value -match '@') { break }
      }
    }
  }
  if (-not $value -or $value -notmatch '@') { $value = $Fallback }
  $value = $value.Trim()
  if ($value -notmatch '^[^\s<>@]+@[^\s<>@]+$') { return '' }
  return $value.ToLowerInvariant()
}

function Get-DeliverySender($Item) {
  $value = Get-CanonicalSmtp $Item.Sender ([string]$Item.SenderEmailAddress)
  if (-not $value) {
    foreach ($tag in @('0x5D01001F','0x5D01001E')) {
      try { $value = Get-CanonicalSmtp $null ([string]$Item.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/' + $tag)) } catch { }
      if ($value) { break }
    }
  }
  return $value
}

function Get-DeliveryFileHash([string]$LiteralPath) {
  # Avoid depending on the script-backed Get-FileHash module on restricted hosts.
  $stream = [IO.File]::OpenRead($LiteralPath)
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()
  } finally { $sha.Dispose(); $stream.Dispose() }
}

function Test-DeliveryItem($Item, $Folder, [string]$Owner, [string]$Subject,
                           [string]$FileName, [string]$FileHash, [long]$FileSize,
                           [datetime]$Start, [datetime]$End, [string]$Scratch) {
  if ($Item.Class -ne 43 -or [string]$Item.Subject -cne $Subject) { return @{ state='skip' } }
  $utc = $Folder.PropertyAccessor.LocalTimeToUTC($Item.ReceivedTime)
  if ($utc -lt $Start -or $utc -gt $End) { return @{ state='skip' } }
  $sender = Get-DeliverySender $Item
  if (-not $sender) { return @{ state='unreadable' } }
  if ($sender -cne $Owner) { return @{ state='skip' } }
  # This transport sends to one owner only. Never accept CC/BCC or a display name.
  if ($Item.Recipients.Count -ne 1) { return @{ state='skip' } }
  $recipient = $Item.Recipients.Item(1)
  $recipientSmtp = Get-CanonicalSmtp $recipient.AddressEntry ([string]$recipient.Address)
  if (-not $recipientSmtp) { return @{ state='unreadable' } }
  if ($recipient.Type -ne 1 -or $recipientSmtp -cne $Owner) { return @{ state='skip' } }
  if ($Item.Attachments.Count -ne 1) { return @{ state='mismatch' } }
  $attached = $Item.Attachments.Item(1)
  if ([string]$attached.FileName -cne $FileName -or [long]$attached.Size -ne $FileSize) {
    return @{ state='mismatch' }
  }
  $temporary = Join-Path $Scratch (([guid]::NewGuid().ToString('N')) + '.attachment')
  try {
    $attached.SaveAsFile($temporary)
    if ((Get-DeliveryFileHash $temporary) -cne $FileHash) {
      return @{ state='mismatch' }
    }
    # Raw COM identifiers stay local; only an opaque digest reaches audit logs.
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
      $receipt = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes(
        [string]$Folder.StoreID + ':' + [string]$Item.EntryID)))).Replace('-','').ToLowerInvariant()
    } finally { $hash.Dispose() }
    return @{ state='matched'; receivedAt=$utc.ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'"); id=$receipt }
  } finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force }
  }
}

# Validate the bounded read request before connecting to Outlook.
if ($Mode -eq 'confirm') {
  if ($RunId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' -or
      $AttachmentSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid delivery identity' }
  if ($StartUtc -notmatch '(Z|[+-]\d{2}:\d{2})$' -or $EndUtc -notmatch '(Z|[+-]\d{2}:\d{2})$') {
    throw 'An aware delivery window is required'
  }
  $start = [DateTimeOffset]::Parse($StartUtc).UtcDateTime
  $end = [DateTimeOffset]::Parse($EndUtc).UtcDateTime
  if ($start -ge $end -or ($end - $start).TotalDays -gt 30 -or
      $end -gt [DateTime]::UtcNow.AddMinutes(2)) { throw 'Invalid delivery window' }
  if (-not (Test-Path -LiteralPath $Attachment -PathType Leaf)) { throw 'Report missing' }
  $reportFile = Get-Item -LiteralPath $Attachment
  if ($reportFile.Length -gt 50MB -or
      (Get-DeliveryFileHash $Attachment) -cne $AttachmentSha256) {
    throw 'Report changed before delivery verification'
  }
}
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
} elseif ($Mode -eq 'confirm') {
  $owner = $Mailbox.Trim().ToLowerInvariant()
  $subject = "[ARIADNE] 주간업무보고 $RunId"
  $state = @{ scanned=0; folders=0; limited=$false; incomplete=$false; mismatch=$false; matched=$null }
  $scratch = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ResultPath))
  function Read-DeliveryFolder($Folder, [int]$Depth) {
    if ($null -ne $state.matched) { return }
    if ($state.folders -ge 50 -or $Depth -gt 8) { $state.limited=$true; return }
    $state.folders++
    try {
      # Filter both UTC receipt time and exact subject before examining any item.
      $dateUri = 'http://schemas.microsoft.com/mapi/proptag/0x0E060040'
      $subjectUri = 'http://schemas.microsoft.com/mapi/proptag/0x0037001F'
      $filter = '@SQL="' + $dateUri + '" >= ' + "'" + $start.ToString('yyyy-MM-dd HH:mm:ss') +
        "' AND " + '"' + $dateUri + '" <= ' + "'" + $end.AddSeconds(1).ToString('yyyy-MM-dd HH:mm:ss') +
        "' AND " + '"' + $subjectUri + '" = ' + "'" + $subject.Replace("'", "''") + "'"
      $items = $Folder.Items.Restrict($filter)
      $null = $items.Sort('[ReceivedTime]', $true)
      foreach ($item in $items) {
        if ($state.scanned -ge 200) { $state.limited=$true; return }
        $state.scanned++
        try {
          $candidate = Test-DeliveryItem $item $Folder $owner $subject $reportFile.Name $AttachmentSha256 $reportFile.Length $start $end $scratch
          if ($candidate.state -eq 'matched') { $state.matched=$candidate; return }
          if ($candidate.state -eq 'mismatch') { $state.mismatch=$true }
          if ($candidate.state -eq 'unreadable') { $state.incomplete=$true }
        } catch { $state.incomplete=$true }
      }
      foreach ($child in $Folder.Folders) { Read-DeliveryFolder $child ($Depth + 1) }
    } catch { $state.incomplete=$true }
  }
  # Inbox and children only: Sent Items never prove delivery.
  Read-DeliveryFolder ($account.DeliveryStore.GetDefaultFolder(6)) 0
  if ($null -ne $state.matched) {
    $result = @{ status='confirmed'; delivered=$true; reason='matched';
      receivedAt=$state.matched.receivedAt; id=$state.matched.id }
  } else {
    $reason = if ($state.limited) { 'scan_limit' } elseif ($state.incomplete) { 'scan_incomplete' }
      elseif ($state.mismatch) { 'attachment_mismatch' } else { 'not_found' }
    $status = if ($reason -eq 'not_found') { 'pending' } else { 'review_required' }
    $result = @{ status=$status; delivered=$false; reason=$reason; receivedAt=$null; id=$null }
  }
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
