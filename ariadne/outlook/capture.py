"""Bounded, ephemeral selection receipts; never retain credentials or mail bodies."""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from threading import RLock
import time
from uuid import UUID, uuid4

from .errors import OutlookError

PREVIEW_TTL = 10 * 60
MAX_PREVIEWS = 5
MAX_PREVIEW_IDS = 250
MAX_SELECTION = 10
IMPORT_TIMEOUT = 60


def selection_error() -> OutlookError:
    return OutlookError("OUTLOOK_SELECTION_INVALID", "현재 조회 결과에서 메일을 1~10건 선택해 주세요. 중복 선택은 허용하지 않습니다.")


def validate_selection(message_ids) -> tuple[str, ...]:
    if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= MAX_SELECTION:
        raise selection_error()
    if any(not isinstance(value, str) or not value or len(value) > 2048 for value in message_ids):
        raise selection_error()
    if len(set(message_ids)) != len(message_ids):
        raise selection_error()
    return tuple(message_ids)


def expired_error() -> OutlookError:
    return OutlookError("OUTLOOK_PREVIEW_EXPIRED", "조회 결과의 저장 가능 시간이 만료되었습니다. 메일을 다시 조회해 주세요.", 410)


def mailbox_id(tenant_id: str, home_account_id: str) -> str:
    # A structured encoding prevents ambiguous concatenations. Names and email
    # addresses are mutable, so they never define mailbox ownership.
    identity = json.dumps([tenant_id, home_account_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Preview:
    generation: int
    config: tuple[str, str]
    home_account_id: str
    folder: str
    fetched_at: str
    messages: tuple[tuple[str, str], ...]
    expires_at: float

    def selected(self, message_ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        available = dict(self.messages)
        if any(message_id not in available for message_id in message_ids):
            raise selection_error()
        selected = tuple((message_id, available[message_id]) for message_id in message_ids)
        if any(not key for _, key in selected):
            raise OutlookError("OUTLOOK_MESSAGE_CHANGED", "메일 버전을 확인할 수 없습니다. 메일을 다시 조회한 뒤 저장해 주세요.", 409)
        return selected


class PreviewCache:
    """Lock order: callers may hold their store lock; this lock never calls out."""

    def __init__(self):
        self._entries: OrderedDict[str, Preview] = OrderedDict()
        self._lock = RLock()

    def _prune(self, now: float) -> None:
        for key, preview in tuple(self._entries.items()):
            if preview.expires_at <= now:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def remember(self, state: dict, result: dict, folder: str) -> str:
        # Copy only immutable IDs/versions and connection identity from trusted
        # server data. Mutating the public result cannot alter a receipt.
        messages = {}
        for item in result.get("items", []):
            message_id, change_key = item.get("id"), item.get("changeKey", "")
            if not isinstance(message_id, str) or not message_id:
                continue
            if len(message_id) > 2048 or not isinstance(change_key, str) or len(change_key) > 2048:
                raise OutlookError("OUTLOOK_GRAPH_INVALID_RESPONSE", "Outlook 조회 결과를 확인하지 못했습니다. 다시 조회해 주세요.", 502)
            messages[message_id] = change_key
        if len(messages) > MAX_PREVIEW_IDS:
            raise OutlookError("OUTLOOK_GRAPH_INVALID_RESPONSE", "Outlook 조회 결과의 범위를 초과했습니다. 조회 범위를 줄여 주세요.", 502)
        now = time.monotonic()
        preview = Preview(
            generation=state["generation"],
            config=(state["config"]["clientId"], state["config"]["tenantId"]),
            home_account_id=state["account"]["homeAccountId"],
            folder=folder,
            fetched_at=result.get("fetchedAt", ""),
            messages=tuple(messages.items()),
            expires_at=now + PREVIEW_TTL,
        )
        with self._lock:
            self._prune(now)
            while self._entries and (
                len(self._entries) >= MAX_PREVIEWS
                or sum(len(entry.messages) for entry in self._entries.values()) + len(messages) > MAX_PREVIEW_IDS
            ):
                self._entries.popitem(last=False)
            preview_id = str(uuid4())
            self._entries[preview_id] = preview
            return preview_id

    def get(self, preview_id) -> Preview:
        if not isinstance(preview_id, str) or len(preview_id) != 36:
            raise OutlookError("OUTLOOK_PREVIEW_INVALID", "유효한 조회 결과를 선택해 주세요.")
        try:
            if str(UUID(preview_id)) != preview_id:
                raise ValueError
        except ValueError:
            raise OutlookError("OUTLOOK_PREVIEW_INVALID", "유효한 조회 결과를 선택해 주세요.") from None
        with self._lock:
            self._prune(time.monotonic())
            preview = self._entries.get(preview_id)
            if preview is None:
                raise expired_error()
            return preview
