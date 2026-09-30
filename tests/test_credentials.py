import ctypes
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from ariadne.credentials import (
    CredentialService, CredentialUnavailable, MAX_CREDENTIAL_BYTES, WindowsCredentialBackend,
    _CredentialW,
)
from ariadne.models import ValidationError


class MemoryBackend:
    """Test-only adapter. No production fallback may use this implementation."""

    name = "test-memory"

    def __init__(self):
        self.value = None

    def exists(self):
        return self.value is not None

    def read(self):
        return self.value

    def write(self, value):
        self.value = value

    def delete(self):
        self.value = None


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend()
        self.service = CredentialService(self.backend)
        self.key = "synthetic-only-key-00000001"

    def test_recreated_service_loads_saved_key_without_prompt(self):
        self.assertFalse(self.service.status()["configured"])
        self.service.save_openai(self.key)
        restarted = CredentialService(self.backend)
        self.assertTrue(restarted.status()["configured"])
        self.assertEqual(restarted.get_openai_key(), self.key)

    def test_overwrite_and_idempotent_delete(self):
        self.service.save_openai(self.key)
        second = "synthetic-only-key-00000002"
        self.service.save_openai(second)
        self.assertEqual(self.service.get_openai_key(), second)
        self.assertFalse(self.service.delete_openai()["configured"])
        self.assertIsNone(self.service.get_openai_key())
        self.assertFalse(self.service.delete_openai()["configured"])

    def test_public_responses_never_include_key_or_key_fragment(self):
        for status in (self.service.status(), self.service.save_openai(self.key), self.service.status()):
            self.assertEqual(set(status), {"supported", "configured", "backend", "message"})
            self.assertNotIn(self.key, json.dumps(status))
            self.assertNotIn(self.key[-4:], json.dumps(status))
        self.assertIn("아직 확인하지", self.service.status()["message"])

    def test_invalid_keys_never_overwrite_or_echo_supplied_value(self):
        self.service.save_openai(self.key)
        for value in (None, {}, 13, True, "", "short", "x" * (MAX_CREDENTIAL_BYTES + 1),
                      "synthetic key contains spaces", "synthetic-secret\nvalue", "한글" * 16,
                      "synthetic-secret\x00value", "synthetic-secret\x7fvalue"):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValidationError) as error:
                    self.service.save_openai(value)
                self.assertNotIn("synthetic", str(error.exception))
                self.assertEqual(self.service.get_openai_key(), self.key)

    def test_valid_boundary_values_and_no_provider_prefix_assumption(self):
        for length in (16, MAX_CREDENTIAL_BYTES):
            self.service.save_openai("a" * length)
            self.assertEqual(len(self.service.get_openai_key()), length)

    def test_backend_error_is_sanitized_for_all_public_methods(self):
        for action, invoke in (
            ("write", lambda: self.service.save_openai(self.key)),
            ("read", self.service.get_openai_key), ("delete", self.service.delete_openai),
        ):
            with self.subTest(action=action), patch.object(self.backend, action,
                                                          side_effect=RuntimeError(self.key)):
                with self.assertRaises(CredentialUnavailable) as error:
                    invoke()
                self.assertNotIn(self.key, str(error.exception))
        with patch.object(self.backend, "exists", side_effect=RuntimeError(self.key)):
            status = self.service.status()
            self.assertFalse(status["supported"])
            self.assertFalse(status["configured"])
            self.assertNotIn(self.key, json.dumps(status))

    def test_corrupt_saved_value_fails_closed(self):
        self.backend.value = "broken"
        with self.assertRaises(CredentialUnavailable) as error:
            self.service.get_openai_key()
        self.assertNotIn("broken", str(error.exception))

    def test_unsupported_platform_has_no_plaintext_fallback(self):
        with patch("ariadne.credentials.sys.platform", "linux"):
            service = CredentialService()
            status = service.status()
            self.assertFalse(status["supported"])
            self.assertFalse(status["configured"])
            self.assertIn("Windows", status["message"])
            with self.assertRaises(CredentialUnavailable):
                service.save_openai(self.key)
            with self.assertRaises(CredentialUnavailable):
                service.get_openai_key()

    def test_no_application_file_or_database_write(self):
        with TemporaryDirectory() as directory, patch("builtins.open", side_effect=AssertionError("file write")), \
                patch("io.open", side_effect=AssertionError("file write")), \
                patch("sqlite3.connect", side_effect=AssertionError("database write")):
            self.service.save_openai(self.key)
            CredentialService(self.backend).get_openai_key()
            self.service.delete_openai()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_native_read_frees_and_clears_buffer_on_success_and_decode_error(self):
        # A synthetic native allocation validates cleanup even on non-Windows CI.
        for content in (self.key.encode("ascii"), b"\xff" * 20):
            with self.subTest(decodable=content == self.key.encode("ascii")):
                buffer = (ctypes.c_ubyte * len(content))(*content)
                credential = _CredentialW()
                credential.CredentialBlobSize = len(content)
                credential.CredentialBlob = ctypes.cast(buffer, type(credential.CredentialBlob))
                native_pointer = ctypes.pointer(credential)

                def read_native(target, kind, flags, out):
                    self.assertEqual((target, kind, flags), ("ariadne/tests/mock", 1, 0))
                    ctypes.cast(out, ctypes.POINTER(ctypes.POINTER(_CredentialW)))[0] = native_pointer
                    return True

                library = Mock()
                library.CredReadW.side_effect = read_native
                backend = WindowsCredentialBackend("ariadne/tests/mock")
                with patch.object(backend, "_api", return_value=library):
                    if content == self.key.encode("ascii"):
                        self.assertEqual(backend.read(), self.key)
                    else:
                        with self.assertRaises(CredentialUnavailable):
                            backend.read()
                library.CredFree.assert_called_once()
                self.assertEqual(bytes(buffer), bytes(len(content)))


@unittest.skipUnless(sys.platform == "win32", "Native WinCred requires Windows")
class WindowsCredentialIntegrationTests(unittest.TestCase):
    def test_synthetic_key_survives_service_recreation_and_is_removed(self):
        # Never inspect or overwrite the production credential target.
        target = "ariadne/tests/" + uuid4().hex
        backend = WindowsCredentialBackend(target)
        service = CredentialService(backend)
        key = "synthetic-not-an-api-key-" + uuid4().hex
        try:
            self.assertFalse(service.status()["configured"])
            self.assertTrue(service.save_openai(key)["configured"])
            restarted = CredentialService(WindowsCredentialBackend(target))
            self.assertEqual(restarted.get_openai_key(), key)
            restarted.save_openai(key + "-replacement")
            self.assertEqual(service.get_openai_key(), key + "-replacement")
        finally:
            service.delete_openai()
        self.assertIsNone(service.get_openai_key())


if __name__ == "__main__":
    unittest.main()
