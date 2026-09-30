"""No provider calls: verify credential lifecycle, races and the real MSAL adapter boundary."""

from contextlib import contextmanager
from copy import deepcopy
from threading import Event, RLock, Thread
import time
import unittest
from unittest.mock import Mock, patch

from ariadne.outlook.auth import AuthSession, MsalAuth, SCOPES
from ariadne.outlook.errors import OutlookError
from ariadne.outlook.service import OutlookService
from ariadne.outlook.store import empty_state

CLIENT = "6c2f05bb-057d-4cab-b9da-18b3873d7b61"
TENANT = "182a4be3-4531-4653-8e41-cc0c7c74e5e2"
ACCOUNT = {"homeAccountId": "synthetic.account", "username": "synthetic@example.invalid", "name": "가상 사용자"}
SECRET = "synthetic-token-never-for-a-provider"


class MemoryStore:
    supported = True
    message = "test memory only"

    def __init__(self):
        self.state = empty_state()
        self.lock = RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            value = deepcopy(self.state)
            yield value
            self.state = value


class FakeAuth:
    available = True

    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.calls = 0
        self.silent_calls = 0
        self.failure = None

    def interactive(self, config):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError("synthetic test timed out")
        if self.failure:
            raise self.failure
        return AuthSession(SECRET, '{"synthetic":"initial-cache"}', dict(ACCOUNT))

    def silent(self, config, cache, account):
        self.silent_calls += 1
        if self.failure:
            raise self.failure
        return AuthSession(SECRET, '{"synthetic":"refreshed-cache"}', dict(account))


class FakeGraph:
    def __init__(self):
        self.calls = 0
        self.on_read = None

    def validate_query(self, **query):
        if query["limit"] == -1:
            raise OutlookError("OUTLOOK_QUERY_INVALID", "invalid synthetic query")

    def read_messages(self, token, **query):
        assert token == SECRET
        self.calls += 1
        if self.on_read:
            self.on_read()
        return {"items": [{"subject": "synthetic preview"}]}


class OutlookServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.auth = FakeAuth()
        self.graph = FakeGraph()
        self.service = OutlookService(self.store, self.auth, self.graph)

    def tearDown(self):
        self.auth.release.set()
        if self.service._worker:
            self.service._worker.join(3)
            self.assertFalse(self.service._worker.is_alive())

    def connect(self):
        self.service.configure(CLIENT, TENANT)
        self.assertEqual(self.service.start_login()["state"], "connecting")
        self.service._worker.join(3)
        self.assertEqual(self.service.status()["state"], "connected")

    def read(self):
        return self.service.read_messages(folder="inbox", start_date="2026-09-01", end_date="2026-09-30", limit=10)

    def test_status_is_offline_and_exposes_only_public_fields(self):
        self.assertEqual(self.service.status()["state"], "not_configured")
        self.connect()
        status = self.service.status()
        self.assertEqual(set(status), {"supported", "configured", "state", "account", "config", "message"})
        self.assertEqual(status["account"], {"username": ACCOUNT["username"], "name": ACCOUNT["name"]})
        self.assertNotIn(SECRET, repr(status))
        self.assertNotIn("homeAccountId", repr(status))
        self.assertEqual(self.auth.silent_calls, 0)
        self.assertEqual(self.graph.calls, 0)

    def test_config_uuid_only_and_changes_invalidate_old_account(self):
        for invalid in ("common", "organizations", "https://evil.invalid", "../other", None, "00000000-0000-0000-0000-000000000000"):
            with self.subTest(value=invalid), self.assertRaises(OutlookError):
                self.service.configure(CLIENT, invalid)
        self.connect()
        self.assertEqual(self.service.configure(CLIENT.upper(), TENANT)["state"], "connected")
        result = self.service.configure(TENANT, CLIENT)
        self.assertEqual(result["state"], "disconnected")
        self.assertEqual(self.store.state["cache"], "")
        self.assertIsNone(result["account"])

    def test_restart_and_silent_refresh_do_not_reopen_login(self):
        self.connect()
        restarted = OutlookService(self.store, self.auth, self.graph)
        self.assertEqual(restarted.status()["state"], "connected")
        result = restarted.read_messages(folder="inbox", start_date="2026-09-01", end_date="2026-09-30", limit=10)
        self.assertEqual(result["items"][0]["subject"], "synthetic preview")
        self.assertEqual(self.auth.calls, 1)
        self.assertEqual(self.auth.silent_calls, 1)
        self.assertIn("refreshed-cache", self.store.state["cache"])

    def test_disconnect_keeps_configuration_but_wipes_tokens(self):
        self.connect()
        before = self.store.state["generation"]
        result = self.service.disconnect()
        self.assertEqual(result["state"], "disconnected")
        self.assertTrue(result["configured"])
        self.assertEqual(self.store.state["generation"], before + 1)
        self.assertEqual(self.store.state["cache"], "")
        with self.assertRaises(OutlookError):
            self.read()
        self.assertEqual(self.auth.silent_calls, 0)

    def test_stale_login_cannot_resurrect_cross_instance_disconnect(self):
        self.auth.release.clear()
        self.service.configure(CLIENT, TENANT)
        self.service.start_login()
        self.assertTrue(self.auth.entered.wait(1))
        other_process = OutlookService(self.store, FakeAuth(), FakeGraph())
        other_process.disconnect()
        self.auth.release.set()
        self.service._worker.join(3)
        self.assertEqual(self.service.status()["state"], "disconnected")
        self.assertEqual(self.store.state["cache"], "")

    def test_repeated_login_cannot_accumulate_workers(self):
        self.auth.release.clear()
        self.service.configure(CLIENT, TENANT)
        self.service.start_login()
        self.assertTrue(self.auth.entered.wait(1))
        self.service.disconnect()
        with self.assertRaises(OutlookError) as caught:
            self.service.start_login()
        self.assertEqual(caught.exception.code, "OUTLOOK_LOGIN_BUSY")
        self.assertEqual(self.auth.calls, 1)

    def test_abandoned_login_expires_and_late_success_is_discarded(self):
        self.auth.release.clear()
        self.service.configure(CLIENT, TENANT)
        self.service.start_login()
        self.assertTrue(self.auth.entered.wait(1))
        with self.store.transaction() as state:
            state["pendingUntil"] = time.time() - 1
        self.assertEqual(self.service.status()["state"], "disconnected")
        self.auth.release.set()
        self.service._worker.join(3)
        self.assertEqual(self.store.state["cache"], "")

    def test_query_rejected_before_refresh_or_network(self):
        with self.assertRaises(OutlookError):
            self.service.read_messages(folder="inbox", start_date="2026-09-01", end_date="2026-09-30", limit=-1)
        self.assertEqual(self.auth.silent_calls, 0)
        self.assertEqual(self.graph.calls, 0)

    def test_silent_reauth_and_transient_errors_are_distinct(self):
        self.connect()
        self.auth.failure = OutlookError("OUTLOOK_AUTH_UNAVAILABLE", "safe transient", 503)
        with self.assertRaises(OutlookError):
            self.read()
        self.assertEqual(self.service.status()["state"], "connected")
        self.assertTrue(self.store.state["cache"])
        self.auth.failure = OutlookError("OUTLOOK_REAUTH_REQUIRED", "safe reauth", 401)
        with self.assertRaises(OutlookError):
            self.read()
        self.assertEqual(self.service.status()["state"], "reauth_required")
        self.assertEqual(self.store.state["cache"], "")
        self.assertEqual(self.graph.calls, 0)

    def test_graph_logout_race_does_not_release_messages(self):
        self.connect()
        self.graph.on_read = self.service.disconnect
        with self.assertRaises(OutlookError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "OUTLOOK_CONNECTION_CHANGED")

    def test_graph_unauthorized_requests_reauthentication(self):
        self.connect()
        def fail():
            raise OutlookError("OUTLOOK_AUTH_REQUIRED", "safe synthetic", 401)
        self.graph.on_read = fail
        with self.assertRaises(OutlookError):
            self.read()
        self.assertEqual(self.service.status()["state"], "reauth_required")

    def test_background_failure_never_exposes_exception_secrets(self):
        self.auth.failure = RuntimeError(SECRET)
        self.service.configure(CLIENT, TENANT)
        self.service.start_login()
        self.service._worker.join(3)
        self.assertEqual(self.service.status()["state"], "error")
        self.assertNotIn(SECRET, repr(self.service.status()))
        self.assertNotIn(SECRET, repr(self.store.state))

    def test_unsupported_fails_closed(self):
        self.store.supported = False
        self.assertEqual(self.service.status()["state"], "unavailable")
        with self.assertRaises(OutlookError):
            self.service.configure(CLIENT, TENANT)
        self.assertEqual(self.store.state, empty_state())


class MsalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = MsalAuth()
        self.app = Mock()
        self.cache = Mock()
        self.cache.serialize.return_value = "synthetic-cache"
        self.app.get_accounts.return_value = [{"home_account_id": ACCOUNT["homeAccountId"], "username": ACCOUNT["username"]}]

    def test_auth_session_repr_never_includes_tokens_or_serialized_cache(self):
        session = AuthSession(SECRET, "synthetic-cache-containing-refresh-token", dict(ACCOUNT))
        rendered = repr(session)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("synthetic-cache-containing-refresh-token", rendered)
        self.assertNotIn("token=", rendered)
        self.assertNotIn("cache=", rendered)

    def test_interactive_uses_browser_timeout_minimal_scope_and_safe_identity(self):
        self.app.acquire_token_interactive.return_value = {"access_token": SECRET, "refresh_token": "synthetic-refresh", "id_token_claims": {"name": "가상 사용자"}}
        with patch.object(self.adapter, "_app", return_value=(self.app, self.cache)) as factory:
            result = self.adapter.interactive({"clientId": CLIENT, "tenantId": TENANT})
        factory.assert_called_once_with({"clientId": CLIENT, "tenantId": TENANT}, "")
        self.app.acquire_token_interactive.assert_called_once_with(scopes=["Mail.Read"], prompt="select_account", timeout=180)
        self.assertEqual(result.account, ACCOUNT)
        self.assertEqual(result.token, SECRET)
        self.assertNotIn("refresh_token", result.account)

    def test_silent_selects_saved_account_and_allows_msal_to_refresh(self):
        self.app.acquire_token_silent_with_error.return_value = {"access_token": SECRET}
        with patch.object(self.adapter, "_app", return_value=(self.app, self.cache)):
            self.adapter.silent({"clientId": CLIENT, "tenantId": TENANT}, "cached", ACCOUNT)
        self.app.acquire_token_silent_with_error.assert_called_once_with(SCOPES, account=self.app.get_accounts.return_value[0])

    def test_silent_does_not_choose_another_cached_account(self):
        self.app.get_accounts.return_value = [{"home_account_id": "other-account", "username": "other@example.invalid"}]
        with patch.object(self.adapter, "_app", return_value=(self.app, self.cache)), self.assertRaises(OutlookError) as caught:
            self.adapter.silent({}, "cached", ACCOUNT)
        self.assertEqual(caught.exception.code, "OUTLOOK_REAUTH_REQUIRED")
        self.app.acquire_token_silent_with_error.assert_not_called()

    def test_msal_error_and_network_exception_are_redacted(self):
        for failure, expected in (({"error": "invalid_grant", "error_description": SECRET}, "OUTLOOK_REAUTH_REQUIRED"), ({"error": "temporarily_unavailable", "error_description": SECRET}, "OUTLOOK_AUTH_UNAVAILABLE")):
            self.app.acquire_token_silent_with_error.return_value = failure
            with patch.object(self.adapter, "_app", return_value=(self.app, self.cache)), self.assertRaises(OutlookError) as caught:
                self.adapter.silent({}, "cached", ACCOUNT)
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn(SECRET, str(caught.exception))
        self.app.acquire_token_silent_with_error.side_effect = RuntimeError(SECRET)
        with patch.object(self.adapter, "_app", return_value=(self.app, self.cache)), self.assertRaises(OutlookError) as caught:
            self.adapter.silent({}, "cached", ACCOUNT)
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
