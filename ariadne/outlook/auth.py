"""Small MSAL adapter. Access tokens are returned only to the server-side service."""

from dataclasses import dataclass, field
from importlib.util import find_spec

from .errors import OutlookError

SCOPES = ["Mail.Read"]
LOGIN_TIMEOUT = 180


@dataclass(frozen=True)
class AuthSession:
    token: str = field(repr=False)
    cache: str = field(repr=False)
    account: dict


def _reauth() -> OutlookError:
    return OutlookError("OUTLOOK_REAUTH_REQUIRED", "Microsoft 계정에 다시 로그인해 주세요.", 401)


def _check_result(result: dict | None) -> str:
    if not isinstance(result, dict):
        raise _reauth()
    token = result.get("access_token")
    if isinstance(token, str) and token:
        return token
    error = result.get("error")
    if error in {"invalid_grant", "interaction_required", "login_required", "consent_required"}:
        raise _reauth()
    if error in {"access_denied", "authorization_declined"}:
        raise OutlookError("OUTLOOK_ACCESS_DENIED", "로그인이 취소되었거나 조직 정책에서 접근을 허용하지 않았습니다.", 403)
    if error in {"invalid_client", "unauthorized_client", "invalid_scope"}:
        raise OutlookError("OUTLOOK_APP_CONFIGURATION", "앱 등록, 테넌트 및 메일 읽기 권한 설정을 확인해 주세요.", 400)
    if error in {"timeout", "expired_token"}:
        raise OutlookError("OUTLOOK_LOGIN_TIMEOUT", "로그인 시간이 만료되었습니다. 다시 연결해 주세요.", 408)
    raise OutlookError("OUTLOOK_AUTH_UNAVAILABLE", "Microsoft 인증에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.", 503)


class MsalAuth:
    """System-browser authorization code flow with MSAL-managed PKCE and state."""

    @property
    def available(self) -> bool:
        return find_spec("msal") is not None

    def _app(self, config: dict, cache_text: str):
        try:
            import msal

            cache = msal.SerializableTokenCache()
            if cache_text:
                cache.deserialize(cache_text)
            app = msal.PublicClientApplication(
                config["clientId"],
                authority="https://login.microsoftonline.com/" + config["tenantId"],
                token_cache=cache,
                timeout=10,
                enable_pii_log=False,
                app_name="ARIADNE",
            )
            return app, cache
        except Exception:
            raise OutlookError("OUTLOOK_AUTH_UNAVAILABLE", "Microsoft 인증을 준비하지 못했습니다. 연결 설정과 네트워크를 확인해 주세요.", 503) from None

    def interactive(self, config: dict) -> AuthSession:
        app, cache = self._app(config, "")
        try:
            result = app.acquire_token_interactive(
                scopes=SCOPES,
                prompt="select_account",
                timeout=LOGIN_TIMEOUT,
            )
            token = _check_result(result)
            accounts = app.get_accounts()
            # A fresh cache belongs to exactly the account just selected by the user.
            if len(accounts) != 1:
                raise _reauth()
            selected = accounts[0]
            identity = {
                "homeAccountId": selected.get("home_account_id", ""),
                "username": selected.get("username", ""),
                "name": result.get("id_token_claims", {}).get("name") or selected.get("username", ""),
            }
            if not identity["homeAccountId"] or not identity["username"]:
                raise _reauth()
            return AuthSession(token, cache.serialize(), identity)
        except OutlookError:
            raise
        except Exception:
            raise OutlookError("OUTLOOK_AUTH_UNAVAILABLE", "Microsoft 로그인을 완료하지 못했습니다. 다시 시도해 주세요.", 503) from None

    def silent(self, config: dict, cache_text: str, account: dict) -> AuthSession:
        app, cache = self._app(config, cache_text)
        try:
            accounts = [a for a in app.get_accounts() if a.get("home_account_id") == account["homeAccountId"]]
            if len(accounts) != 1:
                raise _reauth()
            result = app.acquire_token_silent_with_error(SCOPES, account=accounts[0])
            token = _check_result(result)
            return AuthSession(token, cache.serialize(), dict(account))
        except OutlookError:
            raise
        except Exception:
            raise OutlookError("OUTLOOK_AUTH_UNAVAILABLE", "Microsoft 인증 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.", 503) from None
