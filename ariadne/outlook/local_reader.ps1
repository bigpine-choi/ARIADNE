# Read-only classic Outlook adapter. JSON stdin/stdout only; no UI or mail writes.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Text($value, [int]$max) {
    $s = [string]$value
    if ($s.Length -gt $max) { return $s.Substring(0, $max) }
    return $s
}
function Stamp($value) {
    $d = [datetime]$value
    if ($d.Year -lt 1900 -or $d.Year -gt 4500) { return '' }
    return $d.ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture)
}
function Prop($item, [string]$tag) {
    try {
        $value = $item.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/' + $tag)
        return ,$value
    }
    catch { return '' }
}
function Change($item) {
    $v = Prop $item '0x65E20102'
    if ($v -is [byte[]]) { return [Convert]::ToBase64String($v) }
    return Stamp $item.LastModificationTime
}
function Address($entry, [string]$fallback) {
    if ($null -eq $entry) { return Text $fallback 500 }
    $smtp = Prop $entry '0x39FE001F'
    if ($smtp) { return Text $smtp 500 }
    return Text $fallback 500
}
function ReadMessage($m) {
    $before = Change $m
    if (-not $before) { throw 'No version' }
    $body = [string]$m.Body
    $recipients = @()
    $recipientCount = $m.Recipients.Count
    for ($n = 1; $n -le [Math]::Min(100, $recipientCount); $n++) {
        $r = $m.Recipients.Item($n)
        if ($r.Type -eq 1) {
            $recipients += @{name=(Text $r.Name 500); address=(Address $r.AddressEntry ([string]$r.Address))}
        }
    }
    $attachments = @()
    $attachmentCount = $m.Attachments.Count
    for ($n = 1; $n -le [Math]::Min(100, $attachmentCount); $n++) {
        $a = $m.Attachments.Item($n)
        $hidden = Prop $a '0x7FFE000B'
        $cid = Prop $a '0x3712001F'
        $kind = if ($a.Type -eq 1) {'file'} elseif ($a.Type -eq 5) {'item'} else {'unknown'}
        $attachments += @{id=[string]$n; name=(Text $a.FileName 2000);
            contentType=(Text (Prop $a '0x370E001F') 500); size=[long]$a.Size;
            isInline=([bool]$hidden -or [bool]$cid); kind=$kind; lastModifiedAt=''; state='not_downloaded'}
    }
    $id = [string]$m.EntryID
    if (-not $id -or $id.Length -gt 2048) { throw 'Invalid ID' }
    $value = @{id=$id; internetMessageId=(Text (Prop $m '0x1035001F') 2048);
        subject=(Text $m.Subject 2000); fromName=(Text $m.SenderName 500);
        fromAddress=(Address $m.Sender ([string]$m.SenderEmailAddress)); toRecipients=@($recipients);
        recipientsTruncated=($recipientCount -gt 100); receivedAt=(Stamp $m.ReceivedTime);
        sentAt=(Stamp $m.SentOn); bodyText=(Text $body 100000); bodyTruncated=($body.Length -gt 100000);
        hasAttachments=($attachmentCount -gt 0); conversationId=(Text $m.ConversationID 2048);
        lastModifiedAt=(Stamp $m.LastModificationTime); changeKey=$before; isDraft=(-not $m.Sent);
        attachments=@($attachments); attachmentsTruncated=($attachmentCount -gt 100)}
    if ((Change $m) -ne $before) { throw 'Changed during capture' }
    return $value
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $app = New-Object -ComObject Outlook.Application
    $session = $app.GetNamespace('MAPI')
    if ($request.operation -eq 'discover') {
        $folders = New-Object 'System.Collections.Generic.List[object]'
        $queue = New-Object 'System.Collections.Generic.Queue[object]'
        $warnings = 0
        $visited = 0
        $truncated = $false
        $stores = $session.Stores
        for ($s = 1; $s -le [Math]::Min(50, $stores.Count); $s++) {
            try {
                $store = $stores.Item($s)
                $queue.Enqueue(@{folder=$store.GetRootFolder(); store=$store; depth=0})
            } catch { $warnings++ }
        }
        if ($stores.Count -gt 50) { $truncated = $true }
        while ($queue.Count -gt 0 -and $visited -lt 2000) {
            $entry = $queue.Dequeue()
            $visited++
            try {
                $f = $entry.folder
                if ($f.DefaultItemType -eq 0 -and $entry.depth -gt 0) {
                    $folders.Add(@{storeId=[string]$entry.store.StoreID; folderId=[string]$f.EntryID;
                        storeName=(Text $entry.store.DisplayName 1000); folderPath=(Text $f.FolderPath 1000)})
                }
                $children = $f.Folders
                if ($entry.depth -lt 16) {
                    for ($c = 1; $c -le $children.Count; $c++) {
                        if (($queue.Count + $visited) -ge 2000) { $truncated = $true; break }
                        $queue.Enqueue(@{folder=$children.Item($c); store=$entry.store; depth=($entry.depth+1)})
                    }
                } elseif ($children.Count -gt 0) { $truncated = $true }
            } catch { $warnings++ }
        }
        if ($queue.Count -gt 0) { $truncated = $true }
        @{ok=$true; folders=@($folders.ToArray()); warnings=$warnings; truncated=$truncated} | ConvertTo-Json -Depth 12 -Compress
    } elseif ($request.operation -eq 'collect') {
        $folder = $session.GetFolderFromID([string]$request.folderId, [string]$request.storeId)
        if ($folder.StoreID -ne $request.storeId -or $folder.EntryID -ne $request.folderId -or $folder.FolderPath -ne $request.folderPath) { throw 'Folder mismatch' }
        $start = [DateTimeOffset]::ParseExact($request.startDate+'T00:00:00+09:00', 'yyyy-MM-ddTHH:mm:sszzz', [Globalization.CultureInfo]::InvariantCulture)
        $end = [DateTimeOffset]::ParseExact($request.endDate+'T00:00:00+09:00', 'yyyy-MM-ddTHH:mm:sszzz', [Globalization.CultureInfo]::InvariantCulture).AddDays(1)
        $sent = $request.basis -eq 'sentitems'
        # DASL date comparisons are UTC; fixed property names and parsed dates.
        $tag = if ($sent) {'0x00390040'} else {'0x0E060040'}
        $uri = 'http://schemas.microsoft.com/mapi/proptag/' + $tag
        $lo = $start.UtcDateTime.ToString('g', [Globalization.CultureInfo]::CurrentCulture)
        $hi = $end.UtcDateTime.ToString('g', [Globalization.CultureInfo]::CurrentCulture)
        $filter = '@SQL="' + $uri + '" >= ''' + $lo + ''' AND "' + $uri + '" < ''' + $hi + ''''
        $items = $folder.Items.Restrict($filter)
        $sort = if ($sent) {'[SentOn]'} else {'[ReceivedTime]'}
        $items.Sort($sort, $true)
        $messages = New-Object 'System.Collections.Generic.List[object]'
        $failed = 0
        $attempted = 0
        $scanned = 0
        $truncated = $false
        $item = $items.GetFirst()
        while ($null -ne $item) {
            $scanned++
            if ($scanned -gt 5000) { $truncated=$true; break }
            if ($item.Class -eq 43) {
                if ($attempted -ge [int]$request.limit) { $truncated=$true; break }
                $attempted++
                try {
                    $actual = if ($sent) {[datetime]$item.SentOn} else {[datetime]$item.ReceivedTime}
                    if ($actual.ToUniversalTime() -lt $start.UtcDateTime -or $actual.ToUniversalTime() -ge $end.UtcDateTime) { throw 'Date mismatch' }
                    $messages.Add((ReadMessage $item))
                } catch { $failed++ }
            }
            $item = $items.GetNext()
        }
        @{ok=$true; messages=@($messages.ToArray()); failed=$failed; truncated=$truncated} | ConvertTo-Json -Depth 15 -Compress
    } else { throw 'Unknown operation' }
} catch {
    # Never emit provider exceptions, mailbox content, addresses, or tokens.
    [Console]::Out.Write('{"ok":false}')
    exit 1
}
# Do not call Outlook.Quit(): the running Outlook belongs to the user.
