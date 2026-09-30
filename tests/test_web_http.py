"""Exercise the local web entry point over real HTTP with synthetic data only."""

from http.client import HTTPConnection
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ariadne.demo import seed_demo
from ariadne.credentials import CredentialService, CredentialUnavailable
from ariadne.repository import Repository
from ariadne.server import create_server


class LocalWebHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.repository = Repository(Path(cls.temporary.name) / "private.sqlite3")
        seed_demo(cls.repository)
        cls.server = create_server(
            cls.repository, port=0,
            credentials=CredentialService(backend=MemoryCredentialBackend()),
        )
        cls.port = cls.server.server_address[1]
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=5)
        cls.repository.close()
        cls.temporary.cleanup()

    def get(self, path, headers=None):
        # HTTPConnection preserves traversal strings that browser clients may normalize.
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_root_serves_utf8_html_including_korean_interface_text(self):
        status, headers, payload = self.get("/")
        self.assertEqual(200, status)
        self.assertEqual("text/html; charset=utf-8", headers["Content-Type"])
        self.assertEqual(len(payload), int(headers["Content-Length"]))
        document = payload.decode("utf-8")
        self.assertIn("<html", document.lower())
        self.assertTrue(any("\uac00" <= character <= "\ud7a3" for character in document))

    def test_browser_assets_are_nonempty_utf8_with_correct_media_types(self):
        for path, media_type in (
            ("/assets/app.css", "text/css"),
            ("/assets/app.js", "text/javascript"),
            ("/assets/outlook.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                status, headers, payload = self.get(path)
                self.assertEqual(200, status)
                self.assertEqual(media_type + "; charset=utf-8", headers["Content-Type"])
                self.assertEqual(len(payload), int(headers["Content-Length"]))
                self.assertTrue(payload.decode("utf-8").strip())

    def test_pages_assets_and_api_disable_caching_and_restrict_browser_resources(self):
        expected_directives = {
            "default-src": {"'none'"},
            "script-src": {"'self'"},
            "style-src": {"'self'"},
            "connect-src": {"'self'"},
            "base-uri": {"'none'"},
            "object-src": {"'none'"},
            "frame-ancestors": {"'none'"},
            "form-action": {"'self'"},
        }
        for path in ("/", "/assets/app.css", "/assets/app.js", "/api/health"):
            with self.subTest(path=path):
                status, headers, _ = self.get(path)
                self.assertEqual(200, status)
                self.assertEqual("no-store", headers["Cache-Control"])
                self.assertEqual("nosniff", headers["X-Content-Type-Options"])
                directives = {}
                for directive in headers["Content-Security-Policy"].split(";"):
                    if directive.strip():
                        name, *values = directive.split()
                        directives[name] = set(values)
                for name, values in expected_directives.items():
                    self.assertEqual(values, directives.get(name), name)

    def test_only_published_resources_are_readable(self):
        for path in (
            "/README.md", "/private.sqlite3", "/ariadne/server.py", "/assets/",
            "/../README.md", "/assets/../../README.md",
            "/%2e%2e/README.md", "/assets/%2e%2e/%2e%2e/private.sqlite3",
            "/assets/%2e%2e%2f%2e%2e%2fREADME.md",
        ):
            with self.subTest(path=path):
                status, headers, payload = self.get(path)
                self.assertEqual(404, status)
                self.assertTrue(headers["Content-Type"].startswith("application/json"))
                self.assertIn("error", json.loads(payload.decode("utf-8")))

    def test_untrusted_host_cannot_read_web_resources(self):
        for path in ("/", "/assets/app.js", "/api/work-items"):
            with self.subTest(path=path):
                status, _, payload = self.get(path, {"Host": "untrusted.example"})
                self.assertEqual(403, status)
                self.assertEqual("HOST_DENIED", json.loads(payload)["error"]["code"])

    def test_foreign_origin_and_cross_site_fetch_cannot_read_web_resources(self):
        for headers in (
            {"Origin": "https://untrusted.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            for path in ("/", "/assets/app.css", "/api/work-items"):
                with self.subTest(path=path, headers=headers):
                    status, _, payload = self.get(path, headers)
                    self.assertEqual(403, status)
                    self.assertEqual("ORIGIN_DENIED", json.loads(payload)["error"]["code"])

    def test_same_origin_browser_requests_preserve_json_api_contract(self):
        for hostname in ("127.0.0.1", "localhost"):
            authority = f"{hostname}:{self.port}"
            with self.subTest(hostname=hostname):
                status, headers, payload = self.get("/api/work-items", {
                    "Host": authority,
                    "Origin": "http://" + authority,
                    "Sec-Fetch-Site": "same-origin",
                })
                self.assertEqual(200, status)
                self.assertEqual("application/json; charset=utf-8", headers["Content-Type"])
                items = json.loads(payload.decode("utf-8"))["items"]
                self.assertEqual(1, len(items))
                self.assertEqual("[가상] 펀드 A 날인본 수령·검토", items[0]["title"])
                self.assertEqual("waiting_external", items[0]["status"])


class MemoryCredentialBackend:
    """Disposable storage; HTTP tests never access an OS keychain or provider."""

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


class OpenAiCredentialHttpTests(unittest.TestCase):
    endpoint = "/api/connections/openai"
    first_key = "sk-test-HTTP-only-first-not-a-real-provider-key"
    second_key = "sk-test-HTTP-only-second-not-a-real-provider-key"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary.name) / "credentials-http.sqlite3")
        self.backend = MemoryCredentialBackend()
        self.start_server(CredentialService(backend=self.backend))

    def start_server(self, service):
        self.server = create_server(self.repository, port=0, credentials=service)
        self.port = self.server.server_address[1]
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)

    def tearDown(self):
        self.stop_server()
        self.repository.close()
        self.temporary.cleanup()

    def request(self, method, path=None, payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        logs = StringIO()
        try:
            with redirect_stderr(logs):
                connection.request(method, path or self.endpoint, body=body, headers=request_headers)
                response = connection.getresponse()
                raw = response.read()
            self.assertEqual("no-store", response.getheader("Cache-Control"))
            self.assertEqual("nosniff", response.getheader("X-Content-Type-Options"))
            for secret in (self.first_key, self.second_key):
                self.assertNotIn(secret, raw.decode("utf-8"))
                self.assertNotIn(secret, str(response.getheaders()))
                self.assertNotIn(secret, logs.getvalue())
                self.assertNotIn(secret, json.dumps(self.repository.audit()))
            return response.status, json.loads(raw.decode("utf-8"))
        finally:
            connection.close()

    def test_key_lifecycle_reports_status_without_returning_the_secret(self):
        status, data = self.request("GET")
        self.assertEqual(200, status)
        self.assertTrue(data["supported"])
        self.assertFalse(data["configured"])
        status, data = self.request("POST", payload={"apiKey": self.first_key})
        self.assertEqual(200, status)
        self.assertTrue(data["configured"])
        self.assertEqual(self.first_key, self.backend.read())

        self.stop_server()
        self.start_server(CredentialService(backend=self.backend))
        status, data = self.request("GET")
        self.assertEqual(200, status)
        self.assertTrue(data["configured"])
        status, data = self.request("POST", payload={"apiKey": self.second_key})
        self.assertEqual(200, status)
        self.assertTrue(data["configured"])
        self.assertEqual(self.second_key, self.backend.read())
        status, data = self.request("DELETE")
        self.assertEqual(200, status)
        self.assertFalse(data["configured"])
        self.assertFalse(self.backend.exists())
        self.assertFalse(self.request("GET")[1]["configured"])
        self.assertEqual([], self.repository.audit())

    def test_rejected_key_payloads_do_not_echo_or_overwrite_the_stored_secret(self):
        self.backend.write(self.first_key)
        for payload in (
            {"apiKey": self.second_key, "extra": "unexpected"},
            {"apiKey": {"secret": self.second_key}},
            {"apiKey": [self.second_key]},
            {"apiKey": ""},
        ):
            with self.subTest(payload_type=type(payload["apiKey"]).__name__):
                status, data = self.request("POST", payload=payload)
                self.assertEqual(400, status)
                self.assertIn("error", data)
                self.assertEqual(self.first_key, self.backend.read())
        self.assertEqual([], self.repository.audit())

    def test_foreign_browser_requests_cannot_save_or_delete_credentials(self):
        self.backend.write(self.first_key)
        for headers in (
            {"Host": "untrusted.example"},
            {"Origin": "https://untrusted.example"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            for method in ("POST", "DELETE"):
                with self.subTest(method=method, headers=headers):
                    payload = {"apiKey": self.second_key} if method == "POST" else None
                    status, _ = self.request(method, payload=payload, headers=headers)
                    self.assertEqual(403, status)
                    self.assertEqual(self.first_key, self.backend.read())

    def test_no_http_route_exposes_the_stored_key(self):
        self.backend.write(self.first_key)
        for path in (
            self.endpoint + "/key", self.endpoint + "/secret",
            "/api/get_openai_key", "/api/credentials", "/credentials.json",
        ):
            with self.subTest(path=path):
                status, data = self.request("GET", path=path)
                self.assertEqual(404, status)
                self.assertIn("error", data)

    def test_unavailable_store_is_reported_and_rejects_mutations_without_secret_leaks(self):
        secret = self.first_key

        class UnavailableBackend:
            name = "test-unavailable"

            def fail(self, *args):
                raise CredentialUnavailable("Unexpected backend detail: " + secret)

            exists = read = write = delete = fail

        self.stop_server()
        self.start_server(CredentialService(backend=UnavailableBackend()))
        status, data = self.request("GET")
        self.assertEqual(200, status)
        self.assertFalse(data["supported"])
        self.assertFalse(data["configured"])
        for method in ("POST", "DELETE"):
            with self.subTest(method=method):
                payload = {"apiKey": self.first_key} if method == "POST" else None
                status, data = self.request(method, payload=payload)
                self.assertEqual(503, status)
                self.assertEqual("CREDENTIAL_STORE_UNAVAILABLE", data["error"]["code"])
        self.assertEqual([], self.repository.audit())


if __name__ == "__main__":
    unittest.main()
