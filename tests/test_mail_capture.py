"""Trusted mail selection, ownership, cancellation and bounded import orchestration."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError
from threading import Event, RLock, Thread
import unittest
from unittest.mock import Mock, patch

from ariadne.outlook.auth import AuthSession
from ariadne.outlook.capture import mailbox_id
from ariadne.outlook.errors import OutlookError
from ariadne.outlook.service import OutlookService
from ariadne.outlook.store import empty_state

CLIENT = "6c2f05bb-057d-4cab-b9da-18b3873d7b61"
TENANT = "182a4be3-4531-4653-8e41-cc0c7c74e5e2"
SECRET = "synthetic-access-token-never-for-a-provider"
CACHE = "synthetic-refresh-cache-never-for-a-provider"
ACCOUNT = {"homeAccountId": "synthetic-account-one", "username": "same@example.invalid", "name": "가상 사용자"}


class MemoryStore:
    supported = True
    message = "synthetic memory only"

    def __init__(self):
        self.lock = RLock()
        self.state = empty_state()
        self.state.update(
            generation=1, config={"clientId": CLIENT, "tenantId": TENANT},
            account=dict(ACCOUNT), cache=CACHE, state="connected",
        )

    @contextmanager
    def transaction(self):
        with self.lock:
            state = deepcopy(self.state)
            yield state
            self.state = state


class FakeAuth:
    available = True

    def __init__(self):
        self.silent_calls = 0
        self.failure = None
        self.on_silent = None

    def silent(self, config, cache, account):
        self.silent_calls += 1
        if self.on_silent:
            self.on_silent()
        if self.failure:
            raise self.failure
        return AuthSession(SECRET, CACHE, deepcopy(account))

    def interactive(self, config):
        return AuthSession(SECRET, CACHE, deepcopy(ACCOUNT))


class FakeGraph:
    def __init__(self):
        self.items = [{"id": "immutable-one", "changeKey": "version-one", "subject": "가상 조회", "bodyText": "private preview body"}]
        self.calls = []
        self.on_capture = None
        self.on_read = None

    def validate_query(self, **query):
        pass

    def read_messages(self, token, **query):
        if self.on_read:
            self.on_read()
        return {"items": deepcopy(self.items), "fetchedAt": "2026-10-01T00:00:00Z", **query}

    def capture_message(self, token, message_id, *, expected_change_key, deadline):
        self.calls.append((token, message_id, expected_change_key, deadline))
        if self.on_capture:
            self.on_capture(message_id)
        return {
            "id": message_id, "changeKey": expected_change_key,
            "subject": "provider-owned subject", "bodyText": "provider-owned complete body",
            "attachments": [], "attachmentsTruncated": False,
        }


class MailCaptureTests(unittest.TestCase):
    def setUp(self):
        self.store, self.auth, self.graph = MemoryStore(), FakeAuth(), FakeGraph()
        self.service = OutlookService(self.store, self.auth, self.graph)
        self.save = Mock(return_value={"created": 1, "unchanged": 0, "revised": 0, "items": []})

    def preview(self):
        return self.service.read_messages(folder="inbox", start_date="2026-10-01", end_date="2026-10-01", limit=10)

    def capture(self, preview=None, ids=None):
        preview = preview if preview is not None else self.preview()
        return self.service.import_messages(preview["previewId"], ids or ["immutable-one"], self.save)

    def assert_error(self, code, call):
        with self.assertRaises(OutlookError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(CACHE, str(caught.exception))

    def test_receipt_contains_only_detached_minimal_immutable_metadata(self):
        result = self.preview()
        preview = self.service._previews.get(result["previewId"])
        rendered = repr(preview)
        for forbidden in (SECRET, CACHE, "private preview body", "가상 조회", ACCOUNT["username"]):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn("homeAccountId", repr(result))
        self.assertNotIn(SECRET, repr(result))
        result["items"][0].update(id="tampered", changeKey="tampered", bodyText="client invention")
        self.assertEqual(preview.messages, (("immutable-one", "version-one"),))
        with self.assertRaises(FrozenInstanceError):
            preview.generation = 2

    def test_selected_capture_uses_provider_content_and_server_identity(self):
        result = self.capture()
        self.assertIs(result, self.save.return_value)
        capture = self.save.call_args.args[0]
        self.assertEqual(set(capture), {"mailbox", "folder", "fetchedAt", "messages"})
        self.assertEqual(capture["mailbox"], {"id": mailbox_id(TENANT, ACCOUNT["homeAccountId"]), "username": ACCOUNT["username"]})
        self.assertEqual(capture["messages"][0]["bodyText"], "provider-owned complete body")
        self.assertEqual(self.graph.calls[0][:3], (SECRET, "immutable-one", "version-one"))
        self.assertEqual(capture["folder"], "inbox")
        self.assertTrue(capture["fetchedAt"].endswith("Z"))
        for forbidden in (SECRET, CACHE, "homeAccountId", "clientId", "tenantId"):
            self.assertNotIn(forbidden, repr(capture))

    def test_same_valid_receipt_can_retry_and_repository_decides_deduplication(self):
        preview = self.preview()
        self.save.side_effect = [{"created": 1, "unchanged": 0}, {"created": 0, "unchanged": 1}]
        self.assertEqual(self.capture(preview)["created"], 1)
        self.assertEqual(self.capture(preview)["unchanged"], 1)
        self.assertEqual(self.save.call_count, 2)
        self.assertEqual(len(self.graph.calls), 2)

    def test_invalid_or_unlisted_selections_never_refresh_or_fetch(self):
        preview = self.preview()
        initial_calls = self.auth.silent_calls
        for ids in (None, [], ["immutable-one"] * 2, ["immutable-one"] * 11, ["another-mail"], [1], [True], [{}], [""], ["x" * 2049], ("immutable-one",)):
            with self.subTest(ids=ids):
                self.assert_error("OUTLOOK_SELECTION_INVALID", lambda: self.service.import_messages(preview["previewId"], ids, self.save))
        self.assertEqual(self.auth.silent_calls, initial_calls)
        self.assertEqual(self.graph.calls, [])
        self.save.assert_not_called()

    def test_invalid_unknown_and_restarted_receipts_are_rejected(self):
        preview = self.preview()
        for receipt in (None, {}, "", "not-a-uuid"):
            self.assert_error("OUTLOOK_PREVIEW_INVALID", lambda: self.service.import_messages(receipt, ["immutable-one"], self.save))
        self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.service.import_messages("00000000-0000-0000-0000-000000000001", ["immutable-one"], self.save))
        restarted = OutlookService(self.store, self.auth, self.graph)
        self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: restarted.import_messages(preview["previewId"], ["immutable-one"], self.save))
        self.save.assert_not_called()

    def test_missing_version_requires_fresh_preview(self):
        self.graph.items[0].pop("changeKey")
        self.assert_error("OUTLOOK_MESSAGE_CHANGED", self.capture)
        self.assertEqual(self.graph.calls, [])
        self.save.assert_not_called()

    def test_receipts_expire_and_oldest_is_evicted_after_five_queries(self):
        with patch("ariadne.outlook.capture.time.monotonic", return_value=100):
            previews = [self.preview() for _ in range(6)]
        self.assertEqual(len(self.service._previews._entries), 5)
        with patch("ariadne.outlook.capture.time.monotonic", return_value=101):
            self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.capture(previews[0]))
            self.capture(previews[1])
        with patch("ariadne.outlook.capture.time.monotonic", return_value=700):
            self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.capture(previews[-1]))
        self.assertEqual(self.save.call_count, 1)

    def test_cache_total_message_bound_even_for_nonstandard_provider_batch(self):
        self.graph.items = [{"id": str(i), "changeKey": "v"} for i in range(150)]
        first, second = self.preview(), self.preview()
        self.assertEqual(len(self.service._previews._entries), 1)
        self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.capture(first, ["1"]))
        self.capture(second, ["1"])
        self.graph.items = [{"id": str(i), "changeKey": "v"} for i in range(251)]
        self.assert_error("OUTLOOK_GRAPH_INVALID_RESPONSE", self.preview)

    def test_connection_changes_clear_local_receipts(self):
        preview = self.preview()
        self.service.configure(CLIENT, TENANT)
        self.capture(preview)  # Saving the same configuration does not log out.
        self.service.configure(TENANT, CLIENT)
        self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.capture(preview))
        self.assertEqual(self.save.call_count, 1)

    def test_login_and_disconnect_clear_local_receipts(self):
        preview = self.preview()
        self.service.start_login()
        self.service._worker.join(2)
        self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.capture(preview))
        preview = self.preview()
        self.service.disconnect()
        self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.capture(preview))
        self.save.assert_not_called()

    def test_cross_instance_logout_invalidates_receipt_before_fetch(self):
        preview = self.preview()
        OutlookService(self.store, self.auth, self.graph).disconnect()
        self.assert_error("OUTLOOK_CONNECTION_CHANGED", lambda: self.capture(preview))
        self.assertEqual(self.graph.calls, [])
        self.save.assert_not_called()

    def test_same_email_different_account_or_tenant_cannot_reuse_receipt(self):
        for mutation in (lambda s: s["account"].update(homeAccountId="different-home-account"), lambda s: s["config"].update(tenantId=CLIENT), lambda s: s.update(generation=s["generation"] + 1)):
            self.setUp()
            preview = self.preview()
            with self.store.transaction() as state:
                mutation(state)
            self.assert_error("OUTLOOK_CONNECTION_CHANGED", lambda: self.capture(preview))
            self.save.assert_not_called()
        self.assertNotEqual(mailbox_id(TENANT, "one"), mailbox_id(TENANT, "two"))
        self.assertNotEqual(mailbox_id(TENANT, "one"), mailbox_id(CLIENT, "one"))
        self.assertNotEqual(mailbox_id("ab", "c"), mailbox_id("a", "bc"))

    def test_logout_during_network_fetch_cannot_commit(self):
        preview = self.preview()
        self.graph.on_capture = lambda _: OutlookService(self.store, self.auth, self.graph).disconnect()
        self.assert_error("OUTLOOK_CONNECTION_CHANGED", lambda: self.capture(preview))
        self.assertEqual(len(self.graph.calls), 1)
        self.save.assert_not_called()

    def test_changed_or_failed_second_message_leaves_whole_batch_unsaved(self):
        self.graph.items.append({"id": "immutable-two", "changeKey": "version-two"})
        preview = self.preview()
        def fail_second(message_id):
            if message_id == "immutable-two":
                raise OutlookError("OUTLOOK_MESSAGE_CHANGED", "safe provider change", 409)
        self.graph.on_capture = fail_second
        self.assert_error("OUTLOOK_MESSAGE_CHANGED", lambda: self.capture(preview, ["immutable-one", "immutable-two"]))
        self.assertEqual(len(self.graph.calls), 2)
        self.save.assert_not_called()

    def test_capture_timeout_and_expiry_during_fetch_block_commit(self):
        now = [100.0]
        with patch("ariadne.outlook.capture.time.monotonic", side_effect=lambda: now[0]):
            preview = self.preview()
            self.graph.on_capture = lambda _: now.__setitem__(0, 161.0)
            self.assert_error("OUTLOOK_IMPORT_TIMEOUT", lambda: self.capture(preview))
            now[0] = 697
            self.graph.on_capture = lambda _: now.__setitem__(0, 701.0)
            self.assert_error("OUTLOOK_PREVIEW_EXPIRED", lambda: self.capture(preview))
        self.save.assert_not_called()

    def test_silent_refresh_time_counts_towards_import_deadline(self):
        now = [100.0]
        with patch("ariadne.outlook.capture.time.monotonic", side_effect=lambda: now[0]):
            preview = self.preview()
            self.auth.on_silent = lambda: now.__setitem__(0, 161.0)
            self.assert_error("OUTLOOK_IMPORT_TIMEOUT", lambda: self.capture(preview))
        self.assertEqual(self.graph.calls, [])
        self.save.assert_not_called()

    def test_provider_unauthorized_marks_reauthentication_and_redacts_failures(self):
        preview = self.preview()
        def unauthorized(_):
            raise OutlookError("OUTLOOK_AUTH_REQUIRED", "safe auth failure", 401)
        self.graph.on_capture = unauthorized
        self.assert_error("OUTLOOK_AUTH_REQUIRED", lambda: self.capture(preview))
        self.assertEqual(self.store.state["state"], "reauth_required")
        self.assertEqual(self.store.state["cache"], "")
        self.save.assert_not_called()

    def test_unexpected_provider_error_is_redacted_and_import_lock_is_released(self):
        preview = self.preview()
        self.graph.on_capture = lambda _: (_ for _ in ()).throw(RuntimeError(SECRET))
        self.assert_error("OUTLOOK_GRAPH_UNAVAILABLE", lambda: self.capture(preview))
        self.graph.on_capture = None
        self.capture(preview)
        self.assertEqual(self.save.call_count, 1)

    def test_database_callback_failure_is_preserved_outside_credential_guard(self):
        class SanitizingStore(MemoryStore):
            @contextmanager
            def transaction(self):
                try:
                    with super().transaction() as state:
                        yield state
                except OutlookError:
                    raise
                except Exception:
                    raise OutlookError("OUTLOOK_STORE_UNAVAILABLE", "synthetic credential sanitizer", 503) from None

        self.store = SanitizingStore()
        self.service = OutlookService(self.store, self.auth, self.graph)
        preview = self.preview()
        database_error = ValueError("synthetic database validation")
        self.save.side_effect = database_error
        with self.assertRaises(ValueError) as caught:
            self.capture(preview)
        self.assertIs(caught.exception, database_error)
        self.save.side_effect = None
        self.capture(preview)
        self.assertEqual(self.save.call_count, 2)

    def test_silent_reauthentication_failure_blocks_fetch_and_commit(self):
        preview = self.preview()
        self.auth.failure = OutlookError("OUTLOOK_REAUTH_REQUIRED", "safe auth failure", 401)
        self.assert_error("OUTLOOK_REAUTH_REQUIRED", lambda: self.capture(preview))
        self.assertEqual(self.store.state["state"], "reauth_required")
        self.assertEqual(self.graph.calls, [])
        self.save.assert_not_called()

    def test_simultaneous_import_is_rejected_without_waiting(self):
        preview = self.preview()
        entered, release = Event(), Event()
        errors = []
        def wait_on_fetch(_):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic test timeout")
        self.graph.on_capture = wait_on_fetch
        def run():
            try:
                self.capture(preview)
            except Exception as error:
                errors.append(error)
        worker = Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assert_error("OUTLOOK_IMPORT_BUSY", lambda: self.capture(preview))
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.save.call_count, 1)

    def test_disconnect_cannot_cross_final_guard_and_database_callback(self):
        preview = self.preview()
        attempted, finished = Event(), Event()
        workers = []
        def disconnect():
            attempted.set()
            OutlookService(self.store, self.auth, self.graph).disconnect()
            finished.set()
        def save_under_guard(capture):
            worker = Thread(target=disconnect)
            workers.append(worker)
            worker.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(finished.wait(0.05))
            self.assertEqual(self.store.state["state"], "connected")
            return {"saved": True}
        result = self.service.import_messages(preview["previewId"], ["immutable-one"], save_under_guard)
        for worker in workers:
            worker.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(result, {"saved": True})
        self.assertEqual(self.store.state["state"], "disconnected")


if __name__ == "__main__":
    unittest.main()
