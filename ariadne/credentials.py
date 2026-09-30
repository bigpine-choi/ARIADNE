"""Server-side credentials stored in the current Windows user's OS vault.

No secret is persisted in application files, SQLite, or browser storage. The
status response reports storage only, never successful OpenAI authentication.
Python strings cannot be reliably zeroized; native temporary buffers are cleared
where possible and every successful CredRead allocation is freed with CredFree.
"""

import ctypes
from ctypes import wintypes
import sys
from threading import RLock
from typing import Protocol

from .models import ValidationError


OPENAI_TARGET = "ariadne/openai/default"
MAX_CREDENTIAL_BYTES = 2560
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_UNAVAILABLE = "Windows 자격 증명 관리자에 접근할 수 없습니다. Windows 로그인 상태를 확인해 주세요."
_WINDOWS_REQUIRED = "API 키 보관은 Windows에서 실행할 때 지원됩니다. 평문 파일에는 저장하지 않습니다."
_INVALID_KEY = "API 키는 공백 없는 ASCII 문자 16~2560자로 입력해 주세요."


class CredentialUnavailable(RuntimeError):
    """A safe message that never includes credentials or a native exception."""


class CredentialBackend(Protocol):
    name: str

    def exists(self) -> bool: ...
    def read(self) -> str | None: ...
    def write(self, value: str) -> None: ...
    def delete(self) -> None: ...


def _validate_key(value):
    if (not isinstance(value, str) or not 16 <= len(value) <= MAX_CREDENTIAL_BYTES
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise ValidationError(_INVALID_KEY)


class _UnavailableBackend:
    name = "unavailable"

    def exists(self):
        raise CredentialUnavailable(_WINDOWS_REQUIRED)

    def read(self):
        return self.exists()

    def write(self, value):
        self.exists()

    def delete(self):
        self.exists()


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialBackend:
    """WinCred generic credential scoped to this user on this computer.

    The target override exists for isolated integration testing. Production uses
    OPENAI_TARGET. The native library is loaded only when this backend is used.
    """

    name = "windows-credential-manager"

    def __init__(self, target=OPENAI_TARGET):
        self._target = target
        self._library = None

    def _api(self):
        if sys.platform != "win32":
            raise CredentialUnavailable(_WINDOWS_REQUIRED)
        if self._library is None:
            try:
                library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
                credential_pointer = ctypes.POINTER(_CredentialW)
                library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                             wintypes.DWORD, ctypes.POINTER(credential_pointer)]
                library.CredReadW.restype = wintypes.BOOL
                library.CredWriteW.argtypes = [credential_pointer, wintypes.DWORD]
                library.CredWriteW.restype = wintypes.BOOL
                library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
                library.CredDeleteW.restype = wintypes.BOOL
                library.CredFree.argtypes = [ctypes.c_void_p]
                library.CredFree.restype = None
            except (OSError, AttributeError):
                raise CredentialUnavailable(_UNAVAILABLE) from None
            self._library = library
        return self._library

    def _read(self, *, presence_only=False):
        library = self._api()
        pointer = ctypes.POINTER(_CredentialW)()
        if not library.CredReadW(self._target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return False if presence_only else None
            raise CredentialUnavailable(_UNAVAILABLE)
        try:
            credential = pointer.contents
            size = credential.CredentialBlobSize
            if not 16 <= size <= MAX_CREDENTIAL_BYTES or not credential.CredentialBlob:
                raise CredentialUnavailable(_UNAVAILABLE)
            if presence_only:
                return True
            try:
                return ctypes.string_at(credential.CredentialBlob, size).decode("ascii")
            except UnicodeDecodeError:
                raise CredentialUnavailable(_UNAVAILABLE) from None
        finally:
            try:
                if pointer and pointer.contents.CredentialBlob:
                    ctypes.memset(pointer.contents.CredentialBlob, 0,
                                  pointer.contents.CredentialBlobSize)
            finally:
                library.CredFree(pointer)

    def exists(self):
        return self._read(presence_only=True)

    def read(self):
        return self._read()

    def write(self, value):
        _validate_key(value)
        library = self._api()
        buffer = bytearray(value, "ascii")
        native_buffer = (wintypes.BYTE * len(buffer)).from_buffer(buffer)
        credential = _CredentialW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self._target
        credential.CredentialBlobSize = len(buffer)
        credential.CredentialBlob = ctypes.cast(native_buffer, ctypes.POINTER(wintypes.BYTE))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "ARIADNE"
        try:
            if not library.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialUnavailable(_UNAVAILABLE)
        finally:
            ctypes.memset(native_buffer, 0, len(buffer))

    def delete(self):
        library = self._api()
        if not library.CredDeleteW(self._target, _CRED_TYPE_GENERIC, 0):
            if ctypes.get_last_error() != _ERROR_NOT_FOUND:
                raise CredentialUnavailable(_UNAVAILABLE)


class CredentialService:
    """Public status/mutation interface; key loading is server-side only."""

    def __init__(self, backend: CredentialBackend | None = None):
        self._backend = backend if backend is not None else (
            WindowsCredentialBackend() if sys.platform == "win32" else _UnavailableBackend())
        self._lock = RLock()

    def status(self):
        with self._lock:
            try:
                configured = bool(self._backend.exists())
            except Exception:
                return {"supported": False, "configured": False, "backend": self._backend.name,
                        "message": (_WINDOWS_REQUIRED if isinstance(self._backend, _UnavailableBackend)
                                    else _UNAVAILABLE)}
            return {"supported": True, "configured": configured, "backend": self._backend.name,
                    "message": ("키가 저장되어 있습니다. OpenAI 인증 및 호출은 아직 확인하지 않았습니다."
                                if configured else "저장된 OpenAI API 키가 없습니다.")}

    def _call(self, action, *args):
        try:
            return getattr(self._backend, action)(*args)
        except Exception:
            message = _WINDOWS_REQUIRED if isinstance(self._backend, _UnavailableBackend) else _UNAVAILABLE
            raise CredentialUnavailable(message) from None

    def save_openai(self, api_key):
        _validate_key(api_key)
        with self._lock:
            self._call("write", api_key)
            return self.status()

    def delete_openai(self):
        with self._lock:
            self._call("delete")
            return self.status()

    def get_openai_key(self) -> str | None:
        """For a future server-side OpenAI client; never return through HTTP."""
        with self._lock:
            value = self._call("read")
            if value is not None:
                try:
                    _validate_key(value)
                except ValidationError:
                    raise CredentialUnavailable(_UNAVAILABLE) from None
            return value
