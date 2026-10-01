"""Disposable mail-storage browser fixture with no external account or network.

The production OutlookService manages preview grants and captures. Only the
provider adapter, authentication and encrypted connection store are synthetic.
The real Repository and HTTP server persist mail revisions and work links. A
second invocation using the same disposable database demonstrates restarts;
--revision 2 simulates a provider-side mail change without test-only app routes.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from threading import RLock

from ariadne.demo import seed_demo
from ariadne.outlook.auth import AuthSession
from ariadne.outlook.errors import OutlookError
from ariadne.outlook.graph import GraphClient
from ariadne.outlook.service import OutlookService
from ariadne.outlook.store import empty_state
from ariadne.repository import Repository
from ariadne.server import create_server


CLIENT = "11111111-1111-4111-8111-111111111111"
TENANT = "33333333-3333-4333-8333-333333333333"
ACCOUNT = {
    "homeAccountId": "synthetic.mail.browser.account",
    "username": "synthetic@example.invalid", "name": "가상 근거함 계정",
}
TOKEN = "synthetic-offline-token-not-valid-for-any-provider"
SUBJECT = '<img src=x onerror="window.mailXss=1"> 수익자 요청'
ATTACHMENT = '<img src=x onerror="window.mailXss=3">.xlsx'


def message(revision):
    return {
        "id": "synthetic-mail-001", "internetMessageId": "<synthetic-mail-001@example.invalid>",
        "subject": SUBJECT, "fromName": "가상 수익자", "fromAddress": "sender@example.invalid",
        "toRecipients": [{"name": "가상 담당자", "address": "synthetic@example.invalid"}],
        "receivedAt": "2026-09-30T01:00:00Z", "sentAt": "2026-09-30T00:59:00Z",
        "bodyText": f'<script>window.mailXss=2</script>\n합성 메일 본문 버전 {revision}: 분배일 검토 요청',
        "bodyTruncated": False, "recipientsTruncated": False, "hasAttachments": True,
        "conversationId": "synthetic-conversation-001", "changeKey": f"synthetic-change-{revision}",
        "lastModifiedAt": f"2026-09-30T0{revision}:00:00Z", "isDraft": False,
        "attachments": [{
            "id": "synthetic-attachment-001", "name": ATTACHMENT,
            "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size": 2048, "isInline": False, "kind": "file",
            "lastModifiedAt": "2026-09-30T00:59:00Z", "state": "not_downloaded",
        }],
        "attachmentsTruncated": False,
    }


class MemoryStore:
    supported = True
    message = "Synthetic memory-only connection; no real credentials exist."

    def __init__(self):
        self._state = empty_state()
        self._state.update(
            generation=1, state="connected", config={"clientId": CLIENT, "tenantId": TENANT},
            account=deepcopy(ACCOUNT), cache='{"synthetic":"offline"}',
        )
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            state = deepcopy(self._state)
            yield state
            self._state = state


class SyntheticAuth:
    available = True

    def interactive(self, config):
        return AuthSession(TOKEN, '{"synthetic":"offline"}', deepcopy(ACCOUNT))

    def silent(self, config, cache, account):
        return AuthSession(TOKEN, cache, deepcopy(account))


class SyntheticGraph:
    """A bounded provider adapter. Every token remains inside this process."""

    def __init__(self, revision):
        self.revision = revision

    @staticmethod
    def validate_query(**query):
        return GraphClient.validate_query(**query)

    def read_messages(self, token, *, folder, start_date, end_date, limit):
        assert token == TOKEN
        item = message(self.revision)
        # Attachment metadata is fetched only when the selected mail is saved.
        item.pop("attachments")
        item.pop("attachmentsTruncated")
        return {
            "items": [item], "folder": folder, "startDate": start_date,
            "endDate": end_date, "limit": limit, "truncated": False,
            "fetchedAt": "2026-10-01T00:00:00Z", "message": "합성 메일 조회입니다.",
        }

    def capture_message(self, token, message_id, *, expected_change_key, deadline):
        assert token == TOKEN
        item = message(self.revision)
        if message_id != item["id"] or expected_change_key != item["changeKey"]:
            raise OutlookError("OUTLOOK_MESSAGE_CHANGED", "메일이 변경되었습니다. 다시 조회해 주세요.", 409)
        return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--revision", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    with Repository(args.db) as repository:
        seed_demo(repository)
        service = OutlookService(store=MemoryStore(), auth=SyntheticAuth(), graph=SyntheticGraph(args.revision))
        server = create_server(repository, port=0, outlook=service)
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}"}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
