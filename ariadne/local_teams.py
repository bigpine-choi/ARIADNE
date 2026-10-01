"""Optional local UI Automation reader, never a hidden Teams cache/token scraper.

Only the explicitly configured open chat is read. Teams virtualizes history;
copied messages omit sender/date metadata and are always labelled incomplete.
"""
from datetime import datetime, timezone
import hashlib
import re
import time


def keyboard_capture(chat, driver, limit=100):
    """Bounded backward traversal, aborting if foreground/window identity changes.

    Driver is deliberately tiny and injectable; only Ctrl+M, Ctrl+J, Ctrl+C and
    Up are allowed. No text entry, Enter, clipboard paste or message send.
    """
    messages=[]
    previous=None
    repeats=0
    driver.verify(chat)
    with driver.preserve_clipboard():
        driver.key('focus_messages')
        driver.key('newest')
        for _ in range(limit):
            driver.verify(chat)
            sequence=driver.clipboard_sequence()
            driver.key('copy')
            text=driver.read_copied(sequence)
            if not text or not text.strip():
                break
            if text==previous:
                repeats+=1
                if repeats>=3:
                    break
            else:
                repeats=0
                capture=normalize_capture(chat,text)
                messages.extend(capture['messages'])
            previous=text
            driver.key('previous')
    if not messages:
        raise RuntimeError('Teams message copy is unavailable; no other clipboard data was collected')
    return {'messages':messages,'complete':False,
      'warnings':['teams_keyboard_copy_partial','teams_message_dates_unverified'],
      'scope':'Up to 100 copied messages from the configured foreground chat; dates and full-week coverage unverified'}


class WindowsClipboardDriver:
    """Built-in Win32 input/clipboard adapter. No Graph, browser extension or SDK."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes=ctypes
        self.user=ctypes.WinDLL('user32',use_last_error=True)
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.user.GetClipboardData.restype=wintypes.HANDLE
        self.user.GetClipboardData.argtypes=[wintypes.UINT]
        self.user.SetClipboardData.restype=wintypes.HANDLE
        self.user.SetClipboardData.argtypes=[wintypes.UINT,wintypes.HANDLE]
        self.user.GetForegroundWindow.restype=wintypes.HWND
        self.user.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
        self.kernel.GlobalLock.restype=ctypes.c_void_p
        self.kernel.GlobalLock.argtypes=[wintypes.HGLOBAL]
        self.kernel.GlobalUnlock.argtypes=[wintypes.HGLOBAL]
        self.kernel.GlobalSize.restype=ctypes.c_size_t
        self.kernel.GlobalSize.argtypes=[wintypes.HGLOBAL]
        self.kernel.GlobalAlloc.restype=wintypes.HGLOBAL
        self.kernel.GlobalAlloc.argtypes=[wintypes.UINT,ctypes.c_size_t]
        self.kernel.GlobalFree.argtypes=[wintypes.HGLOBAL]
        self.window=self.user.GetForegroundWindow()

    def verify(self,chat):
        current=self.user.GetForegroundWindow()
        buffer=self.ctypes.create_unicode_buffer(1024)
        self.user.GetWindowTextW(current,buffer,1024)
        parts=[p.strip() for p in buffer.value.split('|')]
        if current!=self.window or chat not in parts or not buffer.value.endswith('Microsoft Teams'):
            raise RuntimeError('Teams foreground chat changed; keyboard collection stopped')

    def key(self,action):
        sequences={'focus_messages':[0x11,0x4D],'newest':[0x11,0x4A],
          'copy':[0x11,0x43],'previous':[0x26]}
        keys=sequences[action]
        for key in keys: self.user.keybd_event(key,0,0,0)
        for key in reversed(keys): self.user.keybd_event(key,0,2,0)
        time.sleep(.25)

    def clipboard_sequence(self):
        return self.user.GetClipboardSequenceNumber()

    def _open(self):
        for _ in range(10):
            if self.user.OpenClipboard(None): return
            time.sleep(.05)
        raise RuntimeError('Clipboard is busy')

    def _bytes(self,format_id):
        handle=self.user.GetClipboardData(format_id)
        if not handle: return None
        pointer=self.kernel.GlobalLock(handle)
        if not pointer: raise RuntimeError('Clipboard data is unavailable')
        try: return self.ctypes.string_at(pointer,self.kernel.GlobalSize(handle))
        finally: self.kernel.GlobalUnlock(handle)

    def read_copied(self,before_sequence):
        for _ in range(10):
            if self.clipboard_sequence()!=before_sequence: break
            time.sleep(.05)
        else: return None  # Never treat stale user clipboard as a Teams message.
        self._open()
        try:
            content=self._bytes(13)
            return content.decode('utf-16-le').rstrip('\x00') if content else None
        finally: self.user.CloseClipboard()

    def preserve_clipboard(self):
        from contextlib import contextmanager
        @contextmanager
        def preserve():
            self._open()
            saved={}
            try:
                identifier=self.user.EnumClipboardFormats(0)
                while identifier:
                    # Non-text clipboard objects cannot be losslessly restored here.
                    if identifier not in {1,7,13,16}:
                        raise RuntimeError('Clipboard contains non-text data; Teams capture skipped to preserve it')
                    saved[identifier]=self._bytes(identifier)
                    identifier=self.user.EnumClipboardFormats(identifier)
            finally: self.user.CloseClipboard()
            try:
                yield
            finally:
                self._open()
                try:
                    self.user.EmptyClipboard()
                    for identifier,content in saved.items():
                        if content is None: continue
                        handle=self.kernel.GlobalAlloc(0x2,len(content))
                        if not handle: raise RuntimeError('Clipboard restore allocation failed')
                        pointer=self.kernel.GlobalLock(handle)
                        if not pointer:
                            self.kernel.GlobalFree(handle)
                            raise RuntimeError('Clipboard restore lock failed')
                        self.ctypes.memmove(pointer,content,len(content))
                        self.kernel.GlobalUnlock(handle)
                        if not self.user.SetClipboardData(identifier,handle):
                            self.kernel.GlobalFree(handle)
                            raise RuntimeError('Clipboard restore failed')
                finally: self.user.CloseClipboard()
        return preserve()


def normalize_capture(chat, text, captured_at=None):
    if not chat or not text.strip():
        raise RuntimeError('Teams accessibility text is unavailable')
    stamp = captured_at or datetime.now(timezone.utc).isoformat()
    fingerprint = hashlib.sha256((chat+'\n'+text).encode('utf-8')).hexdigest()
    return {'messages':[{'source_id':'viewport:'+fingerprint,'provider':'teams_local',
        'chat':chat,'body':text,'timestamp':None,'captured_at':stamp,
        'subject':chat,'sender':'','direction':'unknown'}],
        'complete':False,'warnings':['teams_visible_viewport_only','teams_message_dates_unverified'],
        'scope':'Only the configured currently open chat viewport; not the full week'}


def collect(chats):
    if not chats:
        return {'messages':[],'complete':False,'warnings':['teams_scope_not_configured']}
    import os
    if os.name!='nt':
        raise RuntimeError('Local Teams reader requires Windows')
    driver=WindowsClipboardDriver()
    chat=None
    for candidate in chats:
        try:
            driver.verify(candidate)
            chat=candidate
            break
        except RuntimeError:
            continue
    if chat is None:
        raise RuntimeError('A configured Teams chat must be open in the foreground')
    result=keyboard_capture(chat,driver)
    if len(chats)>1:
        result['warnings'].append('teams_other_configured_chats_not_collected')
    return result


def collect_accessibility(chats):
    """Optional read-only alternative on installations exposing a TextPattern."""
    from pywinauto import Desktop
    windows = [w for w in Desktop(backend='uia').windows()
               if re.search(r'Microsoft Teams$',w.window_text())]
    if len(windows)!=1:
        raise RuntimeError('Exactly one unlocked Teams chat window is required')
    window = windows[0]
    title = window.window_text()
    # No broad substring matching that could select a different person/chat.
    parts = [p.strip() for p in title.split('|')]
    match = [chat for chat in chats if chat in parts]
    if len(match)!=1:
        raise RuntimeError('The open Teams chat is outside the configured collection scope')
    documents = window.descendants(control_type='Document')
    text = []
    for document in documents:
        try:
            content = document.iface_text.DocumentRange.GetText(-1)
            if content and content.strip():
                text.append(content)
        except Exception:
            continue
    if not text:
        raise RuntimeError('Teams does not expose readable message text on this computer')
    result = normalize_capture(match[0],'\n'.join(text))
    if len(chats)>1:
        result['warnings'].append('teams_other_configured_chats_not_collected')
    return result
