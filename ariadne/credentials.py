"""Current-user Windows DPAPI vault. No plaintext fallback or key in responses."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile
import threading

from .config import data_directory

class CredentialError(RuntimeError):
    pass

class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

def _crypt(payload, *, decrypt=False):
    if os.name != "nt":
        raise CredentialError("암호화 키 저장은 Windows에서 지원합니다. 다른 OS에서는 환경변수를 사용하세요.")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    incoming = _Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    if decrypt:
        call = crypt.CryptUnprotectData
        call.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        args = (ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        call = crypt.CryptProtectData
        call.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        # UI_FORBIDDEN; intentionally omit LOCAL_MACHINE (current user only).
        args = (ctypes.byref(incoming), "ARIADNE OpenAI key", None, None, None, 1, ctypes.byref(outgoing))
    call.restype = wintypes.BOOL
    if not call(*args):
        raise CredentialError("암호화 키를 읽거나 저장할 수 없습니다. 같은 Windows 계정인지 확인하세요.")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))

class CredentialStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else data_directory() / "credentials"
        self.path = self.directory / "openai.dpapi"
        self._lock = threading.RLock()

    def status(self):
        return {"saved": self.path.is_file(), "environmentConfigured": bool(os.environ.get("OPENAI_API_KEY")),
                "persistentSupported": os.name == "nt", "connectionVerified": False}

    def save(self, key):
        if (not isinstance(key, str) or not 16 <= len(key) <= 512
                or not key.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key)):
            raise CredentialError("API 키 형식을 확인하세요. 입력값은 기록하지 않았습니다.")
        encrypted = _crypt(key.encode("ascii"))
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".key-", delete=False) as file:
                    temporary = Path(file.name)
                    file.write(encrypted)
                    file.flush()
                    os.fsync(file.fileno())
                temporary.replace(self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return self.status()

    def read(self):
        with self._lock:
            if not self.path.exists():
                return None
            try:
                payload = self.path.read_bytes()
                if not 1 <= len(payload) <= 65536:
                    raise ValueError()
                return _crypt(payload, decrypt=True).decode("ascii")
            except CredentialError:
                raise
            except (OSError, ValueError, UnicodeError):
                raise CredentialError("저장된 API 키를 읽을 수 없습니다. 다시 등록하세요.") from None

    def remove(self):
        with self._lock:
            self.path.unlink(missing_ok=True)
        return self.status()

def resolve_openai_key(store=None):
    # Saved key takes priority, so replacing it takes effect on the next request.
    key = (store or CredentialStore()).read() or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise CredentialError("OpenAI API 키를 최초 한 번 등록하세요.")
    return key
