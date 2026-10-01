# Synthetic objects for executing the real reader on Windows without Outlook.
class FakeProperties {
    [object] GetProperty([string]$tag) {
        if ($tag.EndsWith('0x65E20102')) { return [byte[]](1,2,3) }
        if ($tag.EndsWith('0x1035001F')) { return '<mock@example.invalid>' }
        if ($tag.EndsWith('0x39FE001F')) { return 'mock@example.invalid' }
        if ($tag.EndsWith('0x370E001F')) { return 'application/pdf' }
        if ($tag.EndsWith('0x7FFE000B')) { return $false }
        return ''
    }
}
class FakeCollection {
    [object[]]$Values
    [int]$Count
    FakeCollection([object[]]$values) { $this.Values=$values; $this.Count=$values.Count }
    [object] Item([int]$n) { return $this.Values[$n-1] }
}
class FakeItems {
    [object[]]$Values
    [int]$Index = 0
    FakeItems([object[]]$values) { $this.Values=$values }
    [object] Restrict([string]$filter) {
        $lo = ([datetime]'2026-09-29T15:00:00').ToString('g', [Globalization.CultureInfo]::CurrentCulture)
        $hi = ([datetime]'2026-10-01T15:00:00').ToString('g', [Globalization.CultureInfo]::CurrentCulture)
        if (-not $filter.Contains($lo) -or -not $filter.Contains($hi)) { throw 'Expected KST bounds converted to UTC in local format' }
        return $this
    }
    [void] Sort([string]$field, [bool]$descending) {
        if ($field -notin @('[ReceivedTime]','[SentOn]') -or -not $descending) { throw 'Invalid sort' }
    }
    [object] GetFirst() { $this.Index=0; return $this.Values[0] }
    [object] GetNext() { $this.Index++; if ($this.Index -ge $this.Values.Count) { return $null }; return $this.Values[$this.Index] }
}
class FakeStore {
    [string]$StoreID = 'STORE-A'
    [string]$DisplayName = 'Synthetic mailbox'
    [object]$Root
    [object] GetRootFolder() { return $this.Root }
}
class FakeSession {
    [object]$Stores
    [object]$Folder
    [object] GetFolderFromID([string]$folderId, [string]$storeId) {
        if ($folderId -ne 'FOLDER-A' -or $storeId -ne 'STORE-A') { throw 'Unexpected identity' }
        return $this.Folder
    }
}
class FakeApplication {
    [object]$Session
    [object] GetNamespace([string]$name) { if ($name -ne 'MAPI') { throw 'Wrong namespace' }; return $this.Session }
}
$properties = [FakeProperties]::new()
$recipient = [pscustomobject]@{Name='Synthetic recipient';Address='mock@example.invalid';Type=1;AddressEntry=[pscustomobject]@{PropertyAccessor=$properties}}
$attachment = [pscustomobject]@{FileName='synthetic.pdf';Size=1234;Type=1;PropertyAccessor=$properties}
$mail = [pscustomobject]@{Class=43;EntryID='MAIL-A';Body='Synthetic body';Subject='Synthetic subject';
    SenderName='Synthetic sender';SenderEmailAddress='mock@example.invalid';Sender=$recipient.AddressEntry;
    ConversationID='THREAD-A';Sent=$true;PropertyAccessor=$properties;
    ReceivedTime=([datetime]'2026-09-30T01:00:00Z').ToLocalTime();SentOn=([datetime]'2026-09-30T00:59:00Z').ToLocalTime();
    LastModificationTime=([datetime]'2026-09-30T01:00:00Z').ToLocalTime();
    Recipients=[FakeCollection]::new(@($recipient));Attachments=[FakeCollection]::new(@($attachment))}
$folder = [pscustomobject]@{DefaultItemType=0;EntryID='FOLDER-A';StoreID='STORE-A';FolderPath='Synthetic/Inbox';
    Items=[FakeItems]::new(@($mail,$mail));Folders=[FakeCollection]::new(@())}
$root = [pscustomobject]@{DefaultItemType=0;EntryID='ROOT';FolderPath='Synthetic';Folders=[FakeCollection]::new(@($folder))}
$store=[FakeStore]::new();$store.Root=$root
$session=[FakeSession]::new();$session.Stores=[FakeCollection]::new(@($store));$session.Folder=$folder
$global:FakeApplication=[FakeApplication]::new();$global:FakeApplication.Session=$session
