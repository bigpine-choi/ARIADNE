"""Explicit authentication/model lookup without sending business source material."""
from datetime import datetime, timezone
import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import quote

from .credentials import CredentialError, resolve_openai_key


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_MAX_RESPONSE_BYTES = 65536
_MESSAGES = {
    "MODEL_REQUIRED": "사용할 OpenAI 모델을 입력한 뒤 연결을 확인하세요.",
    "MODEL_INVALID": "모델 이름을 확인하세요. URL 대신 OpenAI API 모델 이름을 입력하세요.",
    "KEY_MISSING": "OpenAI API 키가 저장되지 않았습니다. 키를 암호화 저장한 뒤 다시 확인하세요.",
    "KEY_UNREADABLE": "저장된 API 키를 읽을 수 없습니다. 같은 Windows 계정에서 앱을 실행하고 키를 다시 등록하세요.",
    "KEY_INVALID_FORMAT": "저장된 API 키의 형식을 확인할 수 없습니다. 키를 다시 등록하세요.",
    "CONNECTED": "API 인증과 모델 조회를 확인했습니다. 실제 AI 분석과 생성 한도는 별도로 검증해야 합니다.",
    "AUTHENTICATION_FAILED": "OpenAI가 API 키를 인정하지 않았습니다. 키의 유효 여부와 만료·삭제 여부를 확인하세요.",
    "PERMISSION_DENIED": "해당 API 키의 프로젝트·모델 접근 권한을 확인하세요.",
    "MODEL_NOT_FOUND": "해당 모델을 조회할 수 없습니다. 모델 이름과 프로젝트의 접근 권한을 확인하세요.",
    "QUOTA_EXCEEDED": "API 사용 한도를 초과했습니다. OpenAI API 프로젝트의 결제·사용 한도를 확인하세요.",
    "RATE_LIMITED": "API 요청이 일시적으로 제한됐습니다. 잠시 후 다시 확인하세요.",
    "SERVICE_UNAVAILABLE": "OpenAI 서비스가 일시적으로 응답하지 않습니다. 잠시 후 다시 확인하세요.",
    "REQUEST_TIMEOUT": "API 연결 확인 시간이 초과됐습니다. 인터넷·회사 네트워크 연결을 확인하세요.",
    "TLS_ERROR": "보안 연결을 확인할 수 없습니다. 회사 네트워크·보안 인증서 설정을 확인하세요.",
    "NETWORK_ERROR": "OpenAI API에 연결할 수 없습니다. 인터넷·프록시·회사 네트워크 제한을 확인하세요.",
    "INVALID_RESPONSE": "모델 조회 응답을 확인할 수 없습니다. 잠시 후 다시 확인하세요.",
    "CONNECTION_FAILED": "API 연결 확인에 실패했습니다. 설정과 네트워크 상태를 확인하세요.",
}


def validate_model(model):
    """Accept a bounded API model ID, never a URL, path, or request override."""
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model.strip()):
        raise ValueError(_MESSAGES["MODEL_INVALID"])
    return model.strip()


def _result(code, model=""):
    return {
        "ok": code == "CONNECTED", "code": code, "message": _MESSAGES[code],
        "model": model, "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "authentication_model_lookup",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        # A model lookup must not forward the credential to any redirected URL.
        return None


def _http_code(error):
    try:
        if error.code == 401:
            return "AUTHENTICATION_FAILED"
        if error.code == 403:
            return "PERMISSION_DENIED"
        if error.code == 404:
            return "MODEL_NOT_FOUND"
        if error.code == 429:
            try:
                raw = error.read(4097)
                data = json.loads(raw) if len(raw) <= 4096 else None
                details = data.get("error") if isinstance(data, dict) else None
                if isinstance(details, dict) and details.get("code") == "insufficient_quota":
                    return "QUOTA_EXCEEDED"
            except Exception:
                pass
            return "RATE_LIMITED"
        if 500 <= error.code <= 599:
            return "SERVICE_UNAVAILABLE"
        return "CONNECTION_FAILED"
    finally:
        try:
            error.close()
        except Exception:
            pass


def check_connection(model, credential_store, *, opener=None):
    """Look up only the selected model; return fixed safe diagnostics, never secrets."""
    if isinstance(model, str) and not model.strip():
        return _result("MODEL_REQUIRED")
    try:
        model = validate_model(model)
    except ValueError:
        return _result("MODEL_INVALID")

    try:
        key = resolve_openai_key(credential_store)
    except CredentialError:
        try:
            saved = bool(credential_store.status().get("saved"))
        except Exception:
            saved = True
        return _result("KEY_UNREADABLE" if saved else "KEY_MISSING", model)
    except Exception:
        return _result("KEY_UNREADABLE", model)
    if (not isinstance(key, str) or not 16 <= len(key) <= 512 or not key.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key)):
        return _result("KEY_INVALID_FORMAT", model)

    request = urllib.request.Request(
        "https://api.openai.com/v1/models/" + quote(model, safe=""),
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        method="GET",
    )
    try:
        open_request = opener or urllib.request.build_opener(_NoRedirect()).open
        with open_request(request, timeout=15) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return _result("INVALID_RESPONSE", model)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("object") != "model" or data.get("id") != model:
            return _result("INVALID_RESPONSE", model)
    except urllib.error.HTTPError as error:
        return _result(_http_code(error), model)
    except (TimeoutError, socket.timeout):
        return _result("REQUEST_TIMEOUT", model)
    except ssl.SSLError:
        return _result("TLS_ERROR", model)
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            code = "REQUEST_TIMEOUT"
        elif isinstance(error.reason, ssl.SSLError):
            code = "TLS_ERROR"
        else:
            code = "NETWORK_ERROR"
        return _result(code, model)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return _result("INVALID_RESPONSE", model)
    except OSError:
        return _result("NETWORK_ERROR", model)
    except Exception:
        return _result("CONNECTION_FAILED", model)
    return _result("CONNECTED", model)
