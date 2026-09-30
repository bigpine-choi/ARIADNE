"""Windows kernel mutex: no lock file survives a killed or crashed process."""

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path

from .errors import OutlookError

WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
LOCK_TIMEOUT_MS = 30_000


def _lock_error() -> OutlookError:
    return OutlookError(
        "OUTLOOK_STORE_BUSY",
        "Outlook 보안 저장소를 잠그지 못했습니다. 잠시 후 다시 시도해 주세요.",
        503,
    )


def _kernel_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


class WindowsMutex:
    """Path-scoped cross-process mutex, including separate Windows sessions.

    The default Windows security descriptor belongs to the creating user. Global
    mutexes do not need SeCreateGlobalPrivilege (that requirement concerns global
    file mappings and symbolic links). Handles are non-inheritable. Windows
    closes them on process termination; an abandoned owner never leaves a stale
    on-disk lock. The caller re-reads and validates its atomic envelope after entry.
    """

    def __init__(self, path: str | Path, *, timeout_ms: int = LOCK_TIMEOUT_MS):
        canonical = os.path.normcase(str(Path(path).expanduser().resolve()))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.name = "Global\\ARIADNE.Outlook." + digest
        self.timeout_ms = timeout_ms
        self._handle = None
        self._api = None

    def __enter__(self):
        try:
            self._api = _kernel_api()
            handle = self._api.CreateMutexW(None, False, self.name)
            if not handle:
                raise _lock_error()
            self._handle = handle
            result = self._api.WaitForSingleObject(handle, self.timeout_ms)
            if result not in {WAIT_OBJECT_0, WAIT_ABANDONED}:
                raise _lock_error()
            return self
        except Exception:
            if self._handle is not None:
                self._api.CloseHandle(self._handle)
                self._handle = None
            raise _lock_error() from None

    def __exit__(self, exception_type, exception, traceback):
        handle, self._handle = self._handle, None
        released = False
        closed = False
        try:
            released = bool(self._api.ReleaseMutex(handle))
        finally:
            closed = bool(self._api.CloseHandle(handle))
        if exception_type is None and not (released and closed):
            raise _lock_error()
        return False
