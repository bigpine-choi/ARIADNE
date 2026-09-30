"""Outlook HTTP boundaries with synthetic services, never a mailbox or OS vault."""

from contextlib import redirect_stderr
from http.client import HTTPConnection
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from ariadne.demo import seed_demo
from ariadne.outlook.errors import OutlookError
from ariadne.repository import Repository
from ariadne.server import create_server


class FakeOutlookService:
    """A deterministic service boundary; its token must never become an API route."""

    secret = "synthetic-outlook-token-do-not-return-over-http"

    def __init__(self):
        self.calls = []
        self.failure = None
        self.connection = {"supported": True, "configured": False, "state": "unconfigured"}

    def invoke(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.failure is not None:
            raise self.failure

    def status(self):
        self.invoke("status")
        return dict(self.connection)

    def configure(self, client_id, tenant_id):
        self.invoke("configure", client_id, tenant_id)
        self.connection = {"supported": True, "configured": True, "state": "disconnected"}
        return dict(self.connection)

    def start_login(self):
        self.invoke("start_login")
        self.connection["state"] = "connecting"
        return dict(self.connection)

    def disconnect(self):
        self.invoke("disconnect")
        self.connection["state"] = "disconnected"
        return dict(self.connection)

    def read_messages(self, *, folder, start_date, end_date, limit):
        self.invoke("read_messages", folder=folder, start_date=start_date, end_date=end_date, limit=limit)
        return {
            "items": [{"id": "synthetic-message-1", "subject": "[가상] 확인 요청", "body": "검토를 부탁드립니다."}],
            "truncated": False,
        }


class OutlookHttpTests(unittest.TestCase):
    endpoint = "/api/connections/outlook"
    config = {
        "clientId": "11111111-1111-4111-8111-111111111111",
        "tenantId": "22222222-2222-4222-8222-222222222222",
    }
    read_request = {
        "folder": "inbox", "startDate": "2026-09-01", "endDate": "2026-09-30", "limit": 20,
    }

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary.name) / "outlook-http.sqlite3")
        seed_demo(self.repository)
        self.service = FakeOutlookService()
        self.server = create_server(self.repository, port=0, outlook=self.service)
        self.port = self.server.server_address[1]
        self.worker = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.repository.close()
        self.temporary.cleanup()

    def request(self, method, suffix="", *, payload=None, raw=None, headers=None, path=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        body = raw if raw is not None else (
            None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        logs = StringIO()
        try:
            with redirect_stderr(logs):
                connection.request(method, path or self.endpoint + suffix, body=body, headers=request_headers)
                response = connection.getresponse()
                response_body = response.read()
            self.assertEqual("no-store", response.getheader("Cache-Control"))
            self.assertEqual("nosniff", response.getheader("X-Content-Type-Options"))
            self.assertNotIn(self.service.secret, response_body.decode("utf-8"))
            self.assertNotIn(self.service.secret, str(response.getheaders()))
            self.assertNotIn(self.service.secret, logs.getvalue())
            self.assertNotIn(self.service.secret, json.dumps(self.repository.audit()))
            data = json.loads(response_body.decode("utf-8")) if response.getheader("Content-Type", "").startswith("application/json") else response_body
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def snapshot(self):
        return (
            self.repository.list_work_items(), self.repository.list_evidence(),
            self.repository.list_funds(), self.repository.audit(),
        )

    def test_connection_configuration_login_and_disconnect_delegate_without_repository_writes(self):
        before = self.snapshot()
        status, _, data = self.request("GET")
        self.assertEqual(200, status)
        self.assertEqual(self.service.connection, data)
        status, _, data = self.request("POST", payload=self.config)
        self.assertEqual(200, status)
        self.assertTrue(data["configured"])
        status, _, data = self.request("POST", "/login", payload={})
        self.assertEqual(202, status)
        self.assertEqual("connecting", data["state"])
        status, _, data = self.request("DELETE")
        self.assertEqual(200, status)
        self.assertEqual("disconnected", data["state"])
        self.assertEqual([
            ("status", (), {}),
            ("configure", (self.config["clientId"], self.config["tenantId"]), {}),
            ("start_login", (), {}), ("disconnect", (), {}),
        ], self.service.calls)
        self.assertEqual(before, self.snapshot())

    def test_mail_preview_delegates_exact_scope_and_does_not_change_business_data(self):
        before = self.snapshot()
        status, _, data = self.request("POST", "/messages", payload=self.read_request)
        self.assertEqual(200, status)
        self.assertEqual("synthetic-message-1", data["items"][0]["id"])
        self.assertEqual([("read_messages", (), {
            "folder": "inbox", "start_date": "2026-09-01", "end_date": "2026-09-30", "limit": 20,
        })], self.service.calls)
        self.assertEqual(before, self.snapshot())

    def test_strict_json_objects_reject_unknown_or_missing_fields_before_service_calls(self):
        examples = [
            ("", {}), ("", {"clientId": self.config["clientId"]}),
            ("", {**self.config, "clientSecret": self.service.secret}),
            ("", []), ("", "not an object"),
            ("/login", {"token": self.service.secret}), ("/login", []),
            ("/messages", {}),
            ("/messages", {key: value for key, value in self.read_request.items() if key != "limit"}),
            ("/messages", {**self.read_request, "url": "https://untrusted.example/"}),
            ("/messages", []),
        ]
        for suffix, payload in examples:
            with self.subTest(suffix=suffix, kind=type(payload).__name__):
                status, _, data = self.request("POST", suffix, payload=payload)
                self.assertEqual(400, status)
                self.assertEqual("VALIDATION", data["error"]["code"])
        self.assertEqual([], self.service.calls)

    def test_malformed_duplicate_and_nonfinite_json_never_reach_outlook_service(self):
        for raw in (
            b"{", b"null", b'{"clientId":"one","clientId":"two","tenantId":"three"}',
            b'{"clientId":NaN,"tenantId":"three"}', b"\xff",
        ):
            with self.subTest(raw=raw):
                status, _, data = self.request("POST", raw=raw)
                self.assertEqual(400, status)
                self.assertEqual("VALIDATION", data["error"]["code"])
        self.assertEqual([], self.service.calls)

    def test_mutation_content_types_are_checked_before_service_calls(self):
        for suffix, payload in (("", self.config), ("/login", {}), ("/messages", self.read_request)):
            for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
                with self.subTest(suffix=suffix, content_type=content_type):
                    status, _, data = self.request("POST", suffix, payload=payload, headers={"Content-Type": content_type})
                    self.assertEqual(400, status)
                    self.assertEqual("VALIDATION", data["error"]["code"])
        self.assertEqual([], self.service.calls)

    def test_untrusted_hosts_origins_and_cross_site_fetches_cannot_use_any_outlook_route(self):
        for headers in (
            {"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"},
            {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
        ):
            for method, suffix, payload in (
                ("GET", "", None), ("POST", "", self.config),
                ("POST", "/login", {}), ("POST", "/messages", self.read_request),
                ("DELETE", "", None),
            ):
                with self.subTest(method=method, suffix=suffix, headers=headers):
                    status, _, _ = self.request(method, suffix, payload=payload, headers=headers)
                    self.assertEqual(403, status)
        self.assertEqual([], self.service.calls)

    def test_same_origin_requests_with_browser_headers_are_accepted(self):
        for hostname in ("127.0.0.1", "localhost"):
            with self.subTest(hostname=hostname):
                authority = f"{hostname}:{self.port}"
                status, _, data = self.request("POST", "/messages", payload=self.read_request, headers={
                    "Host": authority, "Origin": "http://" + authority, "Sec-Fetch-Site": "same-origin",
                })
                self.assertEqual(200, status)
                self.assertEqual(1, len(data["items"]))
        self.assertEqual(2, len(self.service.calls))

    def test_unsupported_methods_do_not_start_login_or_read_messages(self):
        for suffix, methods in (("", ("PATCH",)), ("/login", ("GET", "DELETE", "PATCH")), ("/messages", ("GET", "DELETE", "PATCH"))):
            for method in methods:
                with self.subTest(method=method, suffix=suffix):
                    status, _, data = self.request(method, suffix, payload={} if method == "PATCH" else None)
                    self.assertEqual(405, status)
                    self.assertEqual("METHOD_DENIED", data["error"]["code"])
        self.assertEqual([], self.service.calls)

    def test_no_route_exposes_tokens_cache_or_secret_material(self):
        for path in (
            self.endpoint + "/token", self.endpoint + "/refresh-token", self.endpoint + "/cache",
            "/api/get_outlook_token", "/api/credentials/outlook", "/outlook-token-cache.json",
        ):
            with self.subTest(path=path):
                status, _, data = self.request("GET", path=path)
                self.assertEqual(404, status)
                self.assertEqual("NOT_FOUND", data["error"]["code"])
        self.assertEqual([], self.service.calls)

    def test_query_parameters_cannot_bypass_strict_post_contract(self):
        for suffix in ("?token=synthetic", "/login?force=true", "/messages?folder=inbox"):
            with self.subTest(suffix=suffix):
                status, _, data = self.request("POST", suffix, payload={})
                self.assertEqual(400, status)
                self.assertEqual("VALIDATION", data["error"]["code"])
        self.assertEqual([], self.service.calls)

    def test_public_outlook_errors_preserve_http_status_without_internal_details(self):
        self.service.failure = OutlookError("OUTLOOK_SIGN_IN_REQUIRED", "다시 로그인해 주세요.", status=401)
        status, _, data = self.request("POST", "/messages", payload=self.read_request)
        self.assertEqual(401, status)
        self.assertEqual("OUTLOOK_SIGN_IN_REQUIRED", data["error"]["code"])
        self.assertEqual("다시 로그인해 주세요.", data["error"]["message"])

    def test_unexpected_provider_failure_is_generic_and_does_not_touch_repository(self):
        before = self.snapshot()
        self.service.failure = RuntimeError("Raw provider response: " + self.service.secret)
        status, _, data = self.request("POST", "/messages", payload=self.read_request)
        self.assertEqual(500, status)
        self.assertEqual("INTERNAL_ERROR", data["error"]["code"])
        self.assertEqual(before, self.snapshot())

    def test_outlook_browser_script_is_served_with_same_origin_security_headers(self):
        status, headers, script = self.request("GET", path="/assets/outlook.js")
        self.assertEqual(200, status)
        self.assertEqual("text/javascript; charset=utf-8", headers["Content-Type"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertTrue(script.decode("utf-8").strip())

    @unittest.skipIf(os.name == "nt", "Do not access a real Windows credential store in HTTP tests")
    def test_default_service_on_non_windows_reports_unavailable_without_fallback_storage(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.server = create_server(self.repository, port=0)
        self.port = self.server.server_address[1]
        self.worker = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.worker.start()
        before = self.snapshot()
        status, _, data = self.request("GET")
        self.assertEqual(200, status)
        self.assertFalse(data["supported"])
        self.assertEqual("unavailable", data["state"])
        for method, suffix, payload in (
            ("POST", "", self.config), ("POST", "/login", {}),
            ("POST", "/messages", self.read_request), ("DELETE", "", None),
        ):
            with self.subTest(method=method, suffix=suffix):
                status, _, data = self.request(method, suffix, payload=payload)
                self.assertEqual(503, status)
                self.assertEqual("OUTLOOK_UNAVAILABLE", data["error"]["code"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
