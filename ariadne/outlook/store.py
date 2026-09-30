"""One user-scoped DPAPI envelope, atomically replaced under a Windows mutex."""

from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import platform
import tempfile
from threading import RLock

from ..config import data_directory
from .errors import OutlookError
from .locking import WindowsMutex

MAX_STORE_BYTES = 16 * 1024 * 1024


def empty_state() -> dict:
    return {
        "version": 1, "generation": 0, "config": None, "account": None,
        "cache": "", "state": "not_configured", "pendingUntil": None,
    }


def _store_error() -> OutlookError:
    return OutlookError("OUTLOOK_STORE_UNAVAILABLE", "Outlook 보안 저장소를 읽거나 저장하지 못했습니다. Windows 계정과 저장 위치를 확인해 주세요.", 503)


def _validate_state(state: object) -> dict:
    if not isinstance(state, dict) or set(state) != set(empty_state()):
        raise _store_error()
    if type(state["version"]) is not int or state["version"] != 1 or type(state["generation"]) is not int or state["generation"] < 0:
        raise _store_error()
    if not isinstance(state["cache"], str) or state["state"] not in {
        "not_configured", "disconnected", "connecting", "connected", "reauth_required", "error",
    }:
        raise _store_error()
    config = state["config"]
    account = state["account"]
    if config is not None and (not isinstance(config, dict) or set(config) != {"clientId", "tenantId"} or not all(isinstance(v, str) for v in config.values())):
        raise _store_error()
    if account is not None and (not isinstance(account, dict) or set(account) != {"homeAccountId", "username", "name"} or not all(isinstance(v, str) for v in account.values())):
        raise _store_error()
    if state["pendingUntil"] is not None and (
        type(state["pendingUntil"]) not in {int, float} or not math.isfinite(state["pendingUntil"])
    ):
        raise _store_error()
    return state


class WindowsOutlookStore:
    """No plaintext fallback. Configuration and tokens share one crash-safe envelope."""

    def __init__(self, path: Path | None = None):
        self.path = (Path(path) if path is not None else data_directory() / "connections" / "outlook.bin").expanduser().resolve()
        self._lock = RLock()
        self._persistence = None
        self._process_lock = WindowsMutex
        self.supported = False
        self.message = "Outlook 연결 정보 보관은 Windows에서 지원합니다."
        if platform.system() != "Windows":
            return
        # Operational state is never written into the checkout, even with a mistaken override.
        source_root = Path(__file__).resolve().parents[2]
        if (source_root / "pyproject.toml").exists() and self.path.resolve().is_relative_to(source_root):
            self.message = "Outlook 보안 저장 위치를 소스 폴더 밖으로 지정해 주세요."
            return
        try:
            from msal_extensions import FilePersistenceWithDataProtection

            self._persistence = FilePersistenceWithDataProtection
            # Check availability without creating files or decrypting account material.
            self._persistence(str(self.path))
            self.supported = True
            self.message = "Windows 사용자 계정으로 암호화하여 보관합니다."
        except Exception:
            self.message = "Outlook 연결 의존성과 Windows 보안 저장소를 확인해 주세요."

    def _read(self) -> dict:
        if not self.path.exists():
            return empty_state()
        if self.path.stat().st_size > MAX_STORE_BYTES:
            raise _store_error()
        return _validate_state(json.loads(self._persistence(str(self.path)).load()))

    def _write(self, state: dict) -> None:
        text = json.dumps(_validate_state(state), ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_STORE_BYTES:
            raise _store_error()
        descriptor, temporary = tempfile.mkstemp(prefix=".outlook-", suffix=".tmp", dir=self.path.parent)
        os.close(descriptor)
        try:
            # The extension encrypts in memory before opening/writing this temporary file.
            self._persistence(temporary).save(text)
            with open(temporary, "r+b") as encrypted:
                os.fsync(encrypted.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @contextmanager
    def transaction(self):
        if not self.supported:
            raise _store_error()
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self._process_lock(self.path):
                    state = self._read()
                    before = deepcopy(state)
                    yield state
                    if state != before:
                        self._write(state)
        except OutlookError:
            raise
        except Exception:
            raise _store_error() from None
