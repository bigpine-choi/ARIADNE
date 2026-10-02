from datetime import datetime
import io
import json
import os
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ariadne.credentials import CredentialError
from ariadne.openai_connection import check_connection, validate_model


KEY = "synthetic-api-key-no-real-access-00001"
MODEL = "synthetic-model"


class FakeVault:
    def __init__(self, key=KEY, error=None, saved=True):
        self.key, self.error, self.saved = key, error, saved

    def read(self):
        if self.error:
            raise self.error
        return self.key

    def status(self):
        return {"saved": self.saved}


class OpenAIConnectionTests(unittest.TestCase):
    def setUp(self):
        # Keep all tests independent of a user's real key or stored credentials.
        self.environment = patch.dict(os.environ, {"OPENAI_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def assert_result(self, result, code):
        self.assertEqual(code, result["code"])
        self.assertEqual(code == "CONNECTED", result["ok"])
        self.assertEqual("authentication_model_lookup", result["scope"])
        self.assertIsNotNone(datetime.fromisoformat(result["checkedAt"]).tzinfo)
        serialized = json.dumps(result)
        self.assertNotIn(KEY, serialized)
        self.assertNotIn("private-error-content", serialized)
        self.assertEqual({"ok", "code", "message", "model", "checkedAt", "scope"}, set(result))

    def fail_network(self, request, **kwargs):
        self.fail("No network request should be made")

    def test_lookup_is_get_with_only_auth_and_model_no_generation(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            self.assertEqual(15, timeout)
            self.assertEqual("GET", request.get_method())
            self.assertEqual("https://api.openai.com/v1/models/" + MODEL, request.full_url)
            self.assertEqual("Bearer " + KEY, request.get_header("Authorization"))
            self.assertIsNone(request.data)
            return io.BytesIO(json.dumps({"id": MODEL, "object": "model", "private": KEY}).encode())
        result = check_connection("  " + MODEL + "  ", FakeVault(), opener=opener)
        self.assert_result(result, "CONNECTED")
        self.assertEqual(MODEL, result["model"])
        self.assertEqual(1, len(seen))

    def test_model_validation_rejects_urls_paths_headers_and_excess_input(self):
        for model in (None, "", "  ", "https://api.openai.com/x", "../model", "a/b", "a\\b", "a?x=1", "a\nprivate-error-content", "a" * 201, "모델"):
            with self.subTest(model=model):
                with self.assertRaises(ValueError) as error:
                    validate_model(model)
                self.assertNotIn("private-error-content", str(error.exception))
                result = check_connection(model, FakeVault(), opener=self.fail_network)
                self.assert_result(result, "MODEL_REQUIRED" if isinstance(model, str) and not model.strip() else "MODEL_INVALID")
                self.assertEqual("", result["model"])

    def test_finetuned_model_id_is_safely_encoded(self):
        model = "ft:synthetic-model:synthetic-org:name:abc123"
        def opener(request, timeout):
            self.assertEqual("https://api.openai.com/v1/models/ft%3Asynthetic-model%3Asynthetic-org%3Aname%3Aabc123", request.full_url)
            return io.BytesIO(json.dumps({"id": model, "object": "model"}).encode())
        self.assert_result(check_connection(model, FakeVault(), opener=opener), "CONNECTED")

    def test_missing_and_unreadable_credentials_stop_before_network(self):
        self.assert_result(check_connection(MODEL, FakeVault(None, saved=False), opener=self.fail_network), "KEY_MISSING")
        self.assert_result(check_connection(MODEL, FakeVault(error=CredentialError("private-error-content")), opener=self.fail_network), "KEY_UNREADABLE")
        self.assert_result(check_connection(MODEL, FakeVault(error=OSError("private-error-content")), opener=self.fail_network), "KEY_UNREADABLE")

    def test_environment_fallback_works_without_persisting_or_returning_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": KEY}):
            def opener(request, timeout):
                self.assertEqual("Bearer " + KEY, request.get_header("Authorization"))
                return io.BytesIO(json.dumps({"id": MODEL, "object": "model"}).encode())
            self.assert_result(check_connection(MODEL, FakeVault(None, saved=False), opener=opener), "CONNECTED")

    def test_invalid_key_header_cannot_make_request(self):
        for key in ("short", KEY + "\nprivate-error-content", "한글" * 20, KEY + " "):
            self.assert_result(check_connection(MODEL, FakeVault(key), opener=self.fail_network), "KEY_INVALID_FORMAT")

    def http_error_result(self, status, body=b'{"error":{"message":"private-error-content"}}'):
        error = HTTPError("https://api.openai.com/v1/models/" + MODEL, status, "private-error-content", {"private": KEY}, io.BytesIO(body))
        def opener(request, timeout):
            raise error
        result = check_connection(MODEL, FakeVault(), opener=opener)
        self.assertTrue(error.fp.closed)
        return result

    def test_auth_permission_model_and_server_errors_have_distinct_safe_reasons(self):
        for status, code in ((401, "AUTHENTICATION_FAILED"), (403, "PERMISSION_DENIED"), (404, "MODEL_NOT_FOUND"), (500, "SERVICE_UNAVAILABLE"), (503, "SERVICE_UNAVAILABLE"), (400, "CONNECTION_FAILED"), (302, "CONNECTION_FAILED")):
            with self.subTest(status=status):
                self.assert_result(self.http_error_result(status), code)

    def test_quota_is_only_classified_from_bounded_allowlisted_code(self):
        self.assert_result(self.http_error_result(429, b'{"error":{"code":"insufficient_quota","message":"private-error-content"}}'), "QUOTA_EXCEEDED")
        for body in (b'{"error":{"code":"rate_limit_exceeded"}}', b'{"error":{"code":"private-error-content"}}', b'not json', b'[]', b'{"error":"insufficient_quota"}', b'{"error":{"code":"insufficient_quota"},"padding":"' + b'x' * 5000 + b'"}'):
            self.assert_result(self.http_error_result(429, body), "RATE_LIMITED")

    def test_timeout_tls_and_network_errors_are_sanitized(self):
        errors = ((TimeoutError("private-error-content"), "REQUEST_TIMEOUT"), (socket.timeout("private-error-content"), "REQUEST_TIMEOUT"), (ssl.SSLError("private-error-content"), "TLS_ERROR"), (URLError(TimeoutError("private-error-content")), "REQUEST_TIMEOUT"), (URLError(ssl.SSLError("private-error-content")), "TLS_ERROR"), (URLError("private-error-content"), "NETWORK_ERROR"), (OSError("private-error-content"), "NETWORK_ERROR"), (RuntimeError("private-error-content"), "CONNECTION_FAILED"))
        for error, code in errors:
            def opener(request, timeout):
                raise error
            with self.subTest(code=code):
                self.assert_result(check_connection(MODEL, FakeVault(), opener=opener), code)

    def test_unexpected_or_oversized_response_cannot_claim_verified(self):
        for raw in (b'not json', b'[]', b'{"id":"another-model","object":"model"}', b'{"id":"synthetic-model","object":"response"}', b'{"id":"synthetic-model"}', b'x' * 65537, b'\xff'):
            def opener(request, timeout):
                return io.BytesIO(raw)
            with self.subTest(raw_length=len(raw)):
                self.assert_result(check_connection(MODEL, FakeVault(), opener=opener), "INVALID_RESPONSE")

    def test_default_transport_rejects_redirects(self):
        with patch("ariadne.openai_connection.urllib.request.build_opener") as builder:
            builder.return_value.open.return_value = io.BytesIO(json.dumps({"id": MODEL, "object": "model"}).encode())
            self.assert_result(check_connection(MODEL, FakeVault()), "CONNECTED")
            handler = builder.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, 302, "private-error-content", {}, "https://example.invalid"))
            self.assertEqual(15, builder.return_value.open.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
