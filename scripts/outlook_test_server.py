"""Disposable HTTP fixture for browser tests; never performs Microsoft login.

This standalone script is not imported by the application. It injects a small
synthetic service into the real HTTP server, keeping test-only behavior outside
production authentication and credential storage.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from threading import RLock
from time import monotonic

from ariadne.demo import seed_demo
from ariadne.outlook.errors import OutlookError
from ariadne.repository import Repository
from ariadne.server import create_server


REAUTH_CLIENT = "22222222-2222-4222-8222-222222222222"
SUBJECT = '<img src=x onerror="window.outlookXss=1">'
BODY = '<script>window.outlookXss=2</script>\n가상 메일 본문입니다.'


class SyntheticOutlookService:
    """Predictable public service contract; no token or credentials exist here."""

    def __init__(self):
        self._lock = RLock()
        self._config = None
        self._account = None
        self._state = "not_configured"
        self._ready_at = None

    def status(self):
        with self._lock:
            if self._state == "connecting" and monotonic() >= self._ready_at:
                self._state = "connected"
                self._account = {"username": "synthetic@example.invalid", "name": "가상 검증 계정"}
            return {
                "supported": True,
                "configured": self._config is not None,
                "state": self._state,
                "account": deepcopy(self._account),
                "config": deepcopy(self._config),
                "message": "합성 계정으로 UI 흐름만 검증합니다. 실제 Microsoft 연결은 수행하지 않습니다.",
            }

    def configure(self, client_id, tenant_id):
        with self._lock:
            self._config = {"clientId": client_id, "tenantId": tenant_id}
            self._account = None
            self._state = "disconnected"
            self._ready_at = None
            return self.status()

    def start_login(self):
        with self._lock:
            if self._config is None:
                raise OutlookError("OUTLOOK_NOT_CONFIGURED", "연결 설정을 먼저 저장해 주세요.", 400)
            self._state = "connecting"
            self._ready_at = monotonic() + 2
            return self.status()

    def disconnect(self):
        with self._lock:
            self._account = None
            self._state = "disconnected" if self._config else "not_configured"
            self._ready_at = None
            return self.status()

    def read_messages(self, *, folder, start_date, end_date, limit):
        with self._lock:
            if self._config and self._config["clientId"] == REAUTH_CLIENT:
                self._state = "reauth_required"
                raise OutlookError("OUTLOOK_REAUTH_REQUIRED", "Microsoft 계정에 다시 로그인해 주세요.", 401)
            if self._state != "connected":
                raise OutlookError("OUTLOOK_REAUTH_REQUIRED", "Microsoft 계정에 다시 로그인해 주세요.", 401)
            return {
                "items": [{
                    "id": "synthetic-message-001", "subject": SUBJECT,
                    "fromName": "가상 발신자", "fromAddress": "sender@example.invalid",
                    "receivedAt": "2026-09-30T01:00:00Z", "sentAt": "2026-09-30T00:59:00Z",
                    "bodyText": BODY, "bodyTruncated": True, "hasAttachments": True,
                    "internetMessageId": "<synthetic-message-001@example.invalid>",
                }],
                "folder": folder, "startDate": start_date, "endDate": end_date,
                "limit": limit, "truncated": True,
                "fetchedAt": "2026-10-01T00:00:00Z",
                "message": "합성 응답: 지정 건수까지만 표시했으며 본문 일부가 생략되었습니다.",
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    if args.db.exists():
        parser.error("Browser fixture requires a new disposable database")
    with Repository(args.db) as repository:
        seed_demo(repository)
        server = create_server(repository, port=0, outlook=SyntheticOutlookService())
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}"}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
