"""Encrypted persistence contracts, atomic failures, and Windows-native DPAPI restart."""

import base64
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from threading import RLock, Thread
import time
import unittest
from unittest.mock import Mock, patch

from ariadne.outlook.errors import OutlookError
from ariadne.outlook.locking import WAIT_ABANDONED, WAIT_OBJECT_0, WAIT_TIMEOUT, WindowsMutex
from ariadne.outlook.store import WindowsOutlookStore, empty_state


class SyntheticPersistence:
    """Test-only framing exercises filesystem behavior; it is not encryption."""

    def __init__(self, location):
        self.path = Path(location)

    def save(self, content):
        self.path.write_bytes(b"TEST-ONLY:" + base64.b64encode(content.encode("utf-8")))

    def load(self):
        content = self.path.read_bytes()
        if not content.startswith(b"TEST-ONLY:"):
            raise RuntimeError("synthetic broken envelope")
        return base64.b64decode(content[10:]).decode("utf-8")


class SyntheticLock:
    """Test-only in-process lock shared by store instances with the same path."""

    locks = {}
    registry_lock = RLock()

    def __init__(self, path):
        with self.registry_lock:
            self.lock = self.locks.setdefault(str(path), RLock())

    def __enter__(self):
        self.lock.acquire()

    def __exit__(self, *args):
        self.lock.release()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "connections" / "outlook.bin"

    def tearDown(self):
        self.temporary.cleanup()

    def synthetic_store(self):
        store = WindowsOutlookStore(self.path)
        store.supported = True
        store._persistence = SyntheticPersistence
        store._process_lock = SyntheticLock
        return store

    def test_unsupported_os_creates_no_files(self):
        with patch("ariadne.outlook.store.platform.system", return_value="Linux"):
            store = WindowsOutlookStore(self.path)
        self.assertFalse(store.supported)
        with self.assertRaises(OutlookError):
            with store.transaction():
                pass
        self.assertFalse(self.path.parent.exists())

    def test_checkout_path_is_rejected(self):
        with patch("ariadne.outlook.store.platform.system", return_value="Windows"):
            store = WindowsOutlookStore(Path(__file__).resolve().parents[1] / "synthetic-do-not-create.bin")
        self.assertFalse(store.supported)
        self.assertFalse(store.path.exists())

    def test_transaction_rollback_and_restart(self):
        store = self.synthetic_store()
        with store.transaction() as state:
            self.assertEqual(state, empty_state())
            state["generation"] = 1
        with self.assertRaises(OutlookError):
            with store.transaction() as state:
                state["generation"] = 2
                raise OutlookError("SYNTHETIC", "synthetic abort")
        with self.synthetic_store().transaction() as restored:
            self.assertEqual(restored["generation"], 1)
        self.assertFalse(Path(str(self.path) + ".lock").exists())

    def test_replace_failure_preserves_previous_envelope(self):
        store = self.synthetic_store()
        with store.transaction() as state:
            state["generation"] = 1
        before = self.path.read_bytes()
        with patch("ariadne.outlook.store.os.replace", side_effect=OSError("synthetic failure")), self.assertRaises(OutlookError):
            with store.transaction() as state:
                state["generation"] = 2
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".outlook-*.tmp")), [])

    def test_corrupt_envelope_not_overwritten_or_fallback_to_plaintext(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"corrupt synthetic envelope")
        store = self.synthetic_store()
        with self.assertRaises(OutlookError) as caught:
            with store.transaction() as state:
                state["generation"] = 1
        self.assertEqual(caught.exception.code, "OUTLOOK_STORE_UNAVAILABLE")
        self.assertEqual(self.path.read_bytes(), b"corrupt synthetic envelope")

    def test_invalid_json_schema_is_not_accepted(self):
        self.path.parent.mkdir()
        SyntheticPersistence(self.path).save(json.dumps({"cache": "synthetic"}))
        with self.assertRaises(OutlookError):
            with self.synthetic_store().transaction():
                pass

    def test_boolean_version_is_not_accepted_as_integer_one(self):
        self.path.parent.mkdir()
        invalid = empty_state()
        invalid["version"] = True
        SyntheticPersistence(self.path).save(json.dumps(invalid))
        with self.assertRaises(OutlookError):
            with self.synthetic_store().transaction():
                pass

    def test_two_instances_do_not_lose_generation_updates(self):
        stores = [self.synthetic_store(), self.synthetic_store()]
        failures = []
        def update(store):
            try:
                for _ in range(10):
                    with store.transaction() as state:
                        state["generation"] += 1
            except Exception as exc:
                failures.append(type(exc).__name__)
        workers = [Thread(target=update, args=(store,)) for store in stores]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        with stores[0].transaction() as state:
            self.assertEqual(state["generation"], 20)

    @unittest.skipUnless(platform.system() == "Windows", "native Windows DPAPI required")
    def test_native_windows_large_cache_encryption_restart_and_disconnect(self):
        store = WindowsOutlookStore(self.path)
        self.assertTrue(store.supported)
        synthetic = "SYNTHETIC-NOT-A-PROVIDER-TOKEN-" * 500
        with store.transaction() as state:
            state["generation"] = 7
            state["cache"] = synthetic
        encrypted = self.path.read_bytes()
        self.assertNotIn(b"SYNTHETIC", encrypted)
        self.assertNotIn(b'"cache"', encrypted)
        with WindowsOutlookStore(self.path).transaction() as restored:
            self.assertEqual(restored["cache"], synthetic)
        # A separate Python process observes the same state and leaves a durable
        # disconnect generation, proving the CAS tombstone survives process restart.
        code = (
            "from pathlib import Path; import sys; "
            "from ariadne.outlook.store import WindowsOutlookStore; "
            "store = WindowsOutlookStore(Path(sys.argv[1])); "
            "exec('with store.transaction() as state:\\n assert state[\"generation\"] == 7\\n state[\"generation\"] = 8\\n state[\"cache\"] = \"\"')"
        )
        run = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        with store.transaction() as restored:
            self.assertEqual(restored["generation"], 8)
            self.assertEqual(restored["cache"], "")

    @unittest.skipUnless(platform.system() == "Windows", "native Windows kernel mutex required")
    def test_native_windows_killed_lock_owner_does_not_block_restart(self):
        store = WindowsOutlookStore(self.path)
        with store.transaction() as state:
            state["generation"] = 5
        ready = Path(self.temporary.name) / "child-ready"
        code = """
from pathlib import Path
from threading import Event
import sys
from ariadne.outlook.store import WindowsOutlookStore
store = WindowsOutlookStore(Path(sys.argv[1]))
with store.transaction() as state:
    state['generation'] = 99
    Path(sys.argv[2]).write_text('ready', encoding='ascii')
    Event().wait(60)
"""
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path), str(ready)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "synthetic child did not acquire the mutex")
            # Retain another handle so Windows reports WAIT_ABANDONED, instead
            # of destroying/recreating the named object after the child dies.
            from ariadne.outlook.locking import _kernel_api
            api = _kernel_api()
            observer = api.CreateMutexW(None, False, WindowsMutex(self.path).name)
            self.assertTrue(observer)
            try:
                child.kill()
                child.wait(timeout=10)
                with WindowsOutlookStore(self.path).transaction() as recovered:
                    self.assertEqual(recovered["generation"], 5)
                    recovered["generation"] = 6
            finally:
                api.CloseHandle(observer)
            with store.transaction() as recovered:
                self.assertEqual(recovered["generation"], 6)
            self.assertFalse(Path(str(self.path) + ".lock").exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


class WindowsMutexTests(unittest.TestCase):
    def native_api(self, outcome=WAIT_OBJECT_0):
        return Mock(**{
            "CreateMutexW.return_value": 41,
            "WaitForSingleObject.return_value": outcome,
            "ReleaseMutex.return_value": True,
            "CloseHandle.return_value": True,
        })

    def test_normal_and_abandoned_owners_are_acquired_then_released(self):
        for outcome in (WAIT_OBJECT_0, WAIT_ABANDONED):
            with self.subTest(outcome=outcome):
                api = self.native_api(outcome)
                with patch("ariadne.outlook.locking._kernel_api", return_value=api):
                    mutex = WindowsMutex("synthetic/path", timeout_ms=250)
                    with mutex:
                        self.assertIsNotNone(mutex._handle)
                api.CreateMutexW.assert_called_once_with(None, False, mutex.name)
                api.WaitForSingleObject.assert_called_once_with(41, 250)
                api.ReleaseMutex.assert_called_once_with(41)
                api.CloseHandle.assert_called_once_with(41)

    def test_timeout_and_wait_failure_close_without_releasing_unowned_mutex(self):
        for outcome in (WAIT_TIMEOUT, 0xFFFFFFFF):
            with self.subTest(outcome=outcome):
                api = self.native_api(outcome)
                with patch("ariadne.outlook.locking._kernel_api", return_value=api), self.assertRaises(OutlookError):
                    with WindowsMutex("synthetic/path"):
                        self.fail("unowned mutex must not enter")
                api.ReleaseMutex.assert_not_called()
                api.CloseHandle.assert_called_once_with(41)

    def test_creation_failure_closes_no_invalid_handle(self):
        api = self.native_api()
        api.CreateMutexW.return_value = None
        with patch("ariadne.outlook.locking._kernel_api", return_value=api), self.assertRaises(OutlookError):
            with WindowsMutex("synthetic/path"):
                pass
        api.WaitForSingleObject.assert_not_called()
        api.CloseHandle.assert_not_called()

    def test_body_exception_still_releases_and_closes_handle(self):
        api = self.native_api()
        with patch("ariadne.outlook.locking._kernel_api", return_value=api), self.assertRaisesRegex(ValueError, "synthetic"):
            with WindowsMutex("synthetic/path"):
                raise ValueError("synthetic")
        api.ReleaseMutex.assert_called_once_with(41)
        api.CloseHandle.assert_called_once_with(41)

    def test_name_uses_path_digest_not_readable_user_path(self):
        mutex = WindowsMutex("synthetic-private-directory/outlook.bin")
        self.assertTrue(mutex.name.startswith("Global\\ARIADNE.Outlook."))
        self.assertNotIn("synthetic-private-directory", mutex.name)
        self.assertEqual(len(mutex.name.rsplit(".", 1)[1]), 64)


if __name__ == "__main__":
    unittest.main()
