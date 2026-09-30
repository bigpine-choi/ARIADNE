"""Connection orchestration with transactional generations and isolated login workers."""

from copy import deepcopy
from threading import RLock, Thread
import time
from uuid import UUID

from .auth import LOGIN_TIMEOUT, MsalAuth
from .errors import OutlookError
from .store import WindowsOutlookStore

MESSAGES = {
    "not_configured": "Outlook 앱 등록 정보를 최초 한 번 설정해 주세요.",
    "disconnected": "Microsoft 계정을 연결해 주세요.",
    "connecting": "열린 Microsoft 로그인 창에서 계정을 선택해 주세요.",
    "connected": "로그인 정보를 보관하고 있습니다. 메일 조회 시 인증을 확인하고 필요한 경우 토큰을 갱신합니다.",
    "reauth_required": "Microsoft 계정에 다시 로그인해 주세요.",
    "error": "Outlook 연결을 완료하지 못했습니다. 앱 등록, 조직 정책 및 네트워크를 확인해 주세요.",
}


def _uuid(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise OutlookError("OUTLOOK_CONFIGURATION_INVALID", f"{label}에 UUID 형식의 ID를 입력해 주세요.")
    try:
        parsed = UUID(value.strip())
    except (ValueError, AttributeError):
        raise OutlookError("OUTLOOK_CONFIGURATION_INVALID", f"{label}에 UUID 형식의 ID를 입력해 주세요.") from None
    if str(parsed) != value.strip().lower() or parsed.int == 0:
        raise OutlookError("OUTLOOK_CONFIGURATION_INVALID", f"{label}에 UUID 형식의 ID를 입력해 주세요.")
    return str(parsed)


class OutlookService:
    def __init__(self, store=None, auth=None, graph=None):
        self.store = store if store is not None else WindowsOutlookStore()
        self.auth = auth if auth is not None else MsalAuth()
        if graph is None:
            from .graph import GraphClient

            graph = GraphClient()
        self.graph = graph
        self._worker = None
        self._worker_lock = RLock()

    def _available(self) -> bool:
        return bool(self.store.supported and self.auth.available)

    def _require_available(self) -> None:
        if not self._available():
            raise OutlookError("OUTLOOK_UNAVAILABLE", "Windows 보안 저장소와 Outlook 연결 의존성을 확인해 주세요.", 503)

    @staticmethod
    def _expire(state: dict) -> None:
        if state["state"] == "connecting" and (state["pendingUntil"] or 0) <= time.time():
            state["generation"] += 1
            state.update(state="disconnected", account=None, cache="", pendingUntil=None)

    @staticmethod
    def _public(state: dict) -> dict:
        account = state["account"]
        return {
            "supported": True,
            "configured": state["config"] is not None,
            "state": state["state"],
            "account": {"username": account["username"], "name": account["name"]} if account else None,
            "config": deepcopy(state["config"]),
            "message": MESSAGES[state["state"]],
        }

    def status(self) -> dict:
        if not self._available():
            return {
                "supported": False, "configured": False, "state": "unavailable", "account": None,
                "config": None, "message": self.store.message if not self.store.supported else "Outlook 연결 의존성을 설치해 주세요.",
            }
        try:
            with self.store.transaction() as state:
                self._expire(state)
                return self._public(state)
        except OutlookError as exc:
            return {"supported": True, "configured": False, "state": "error", "account": None, "config": None, "message": exc.message}

    def configure(self, client_id, tenant_id) -> dict:
        config = {"clientId": _uuid(client_id, "클라이언트 ID"), "tenantId": _uuid(tenant_id, "테넌트 ID")}
        self._require_available()
        with self.store.transaction() as state:
            if state["config"] != config:
                state["generation"] += 1
                state.update(config=config, account=None, cache="", state="disconnected", pendingUntil=None)
            result = self._public(state)
        return result

    def start_login(self) -> dict:
        self._require_available()
        with self._worker_lock:
            # A cancelled browser window can remain until its bounded MSAL timeout.
            # Never accumulate additional live workers for repeated clicks.
            if self._worker is not None and self._worker.is_alive():
                raise OutlookError("OUTLOOK_LOGIN_BUSY", "이전 로그인 창을 완료하거나 닫은 뒤 잠시 후 다시 연결해 주세요.", 409)
            with self.store.transaction() as state:
                self._expire(state)
                if state["config"] is None:
                    raise OutlookError("OUTLOOK_NOT_CONFIGURED", "앱 등록 정보를 먼저 저장해 주세요.", 409)
                if state["state"] == "connecting":
                    raise OutlookError("OUTLOOK_LOGIN_BUSY", "다른 창에서 Microsoft 로그인을 진행 중입니다.", 409)
                state["generation"] += 1
                generation = state["generation"]
                config = deepcopy(state["config"])
                state.update(state="connecting", account=None, cache="", pendingUntil=time.time() + LOGIN_TIMEOUT + 30)
                result = self._public(state)
            self._worker = Thread(target=self._login_worker, args=(config, generation), daemon=True, name="ariadne-outlook-login")
            try:
                self._worker.start()
            except RuntimeError:
                with self.store.transaction() as state:
                    if state["generation"] == generation:
                        state.update(state="error", pendingUntil=None)
                raise OutlookError("OUTLOOK_AUTH_UNAVAILABLE", MESSAGES["error"], 503) from None
        return result

    def _login_worker(self, config: dict, generation: int) -> None:
        try:
            session = self.auth.interactive(config)
            with self.store.transaction() as state:
                self._expire(state)
                if state["generation"] != generation or state["state"] != "connecting" or state["config"] != config:
                    return
                state.update(state="connected", account=dict(session.account), cache=session.cache, pendingUntil=None)
        except Exception:
            # Provider and native exception text may contain sensitive material.
            # Persist only an application-owned state, and never let a worker traceback escape.
            try:
                with self.store.transaction() as state:
                    if state["generation"] == generation and state["state"] == "connecting":
                        state.update(state="error", account=None, cache="", pendingUntil=None)
            except Exception:
                pass

    def disconnect(self) -> dict:
        self._require_available()
        with self.store.transaction() as state:
            # The generation is intentionally retained on disk after local logout.
            # An older process cannot resurrect a completed login after this point.
            state["generation"] += 1
            state.update(account=None, cache="", state="disconnected" if state["config"] else "not_configured", pendingUntil=None)
            result = self._public(state)
        return result

    def read_messages(self, *, folder, start_date, end_date, limit):
        self.graph.validate_query(folder=folder, start_date=start_date, end_date=end_date, limit=limit)
        self._require_available()
        error = None
        with self.store.transaction() as state:
            if state["state"] != "connected" or state["account"] is None:
                raise OutlookError("OUTLOOK_AUTH_REQUIRED", "Microsoft 계정을 먼저 연결해 주세요.", 401)
            generation = state["generation"]
            account = deepcopy(state["account"])
            try:
                session = self.auth.silent(state["config"], state["cache"], account)
                state["cache"] = session.cache
                token = session.token
            except OutlookError as exc:
                error = exc
                if exc.code == "OUTLOOK_REAUTH_REQUIRED":
                    state.update(state="reauth_required", cache="")
            except Exception:
                error = OutlookError("OUTLOOK_AUTH_UNAVAILABLE", "Microsoft 인증을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.", 503)
        if error:
            raise error
        try:
            result = self.graph.read_messages(token, folder=folder, start_date=start_date, end_date=end_date, limit=limit)
        except OutlookError as exc:
            if exc.status == 401:
                with self.store.transaction() as state:
                    if state["generation"] == generation and state["account"] == account:
                        state.update(state="reauth_required", cache="")
            raise
        except Exception:
            raise OutlookError("OUTLOOK_GRAPH_UNAVAILABLE", "Outlook 메일을 조회하지 못했습니다. 잠시 후 다시 시도해 주세요.", 503) from None
        with self.store.transaction() as state:
            if state["generation"] != generation or state["state"] != "connected" or state["account"] != account:
                raise OutlookError("OUTLOOK_CONNECTION_CHANGED", "조회 중 계정 연결이 변경되었습니다. 현재 계정으로 다시 조회해 주세요.", 409)
        return result
