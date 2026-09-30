"""Synthetic Graph contracts and loopback transport; no account or external API."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from ariadne.outlook.errors import OutlookError
from ariadne.outlook.graph import (
    GraphClient, GraphResponse, GraphTransport, MAX_BODY_CHARS,
    MAX_PAGE_BYTES, MAX_PAGES, MAX_PREVIEW_BYTES,
)


TOKEN = "synthetic-graph-token-not-an-account-secret"
BASE = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
QUERY = {"folder": "inbox", "start_date": "2026-09-01", "end_date": "2026-09-30", "limit": 20}


def message(message_id="synthetic-1", **updates):
    result = {
        "id": message_id,
        "internetMessageId": "<synthetic-1@invalid.example>",
        "subject": "[가상] 펀드 자료 요청",
        "from": {"emailAddress": {"name": "가상 담당자", "address": "sender@invalid.example"}},
        "toRecipients": [{"emailAddress": {"name": "가상 사용자", "address": "reader@invalid.example"}}],
        "receivedDateTime": "2026-09-29T23:50:00Z",
        "sentDateTime": "2026-09-29T23:49:00Z",
        "body": {"contentType": "text", "content": "가상 본문. 서류를 확인해 주세요."},
        "hasAttachments": True,
    }
    result.update(updates)
    return result


def response(items=None, next_link=None, *, status=200, headers=None, raw=None):
    page = {"value": [] if items is None else items}
    if next_link is not None:
        page["@odata.nextLink"] = next_link
    return GraphResponse(status, headers or {}, json.dumps(page).encode() if raw is None else raw)


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class GraphQueryTests(unittest.TestCase):
    def test_korean_calendar_days_are_inclusive_and_filter_uses_utc(self):
        transport = FakeTransport(response([message()]))
        result = GraphClient(transport).read_messages(TOKEN, **QUERY)
        url, options = transport.calls[0]
        parsed = urlsplit(url)
        self.assertEqual("/v1.0/me/mailFolders/inbox/messages", parsed.path)
        parameters = parse_qs(parsed.query)
        self.assertEqual(
            ["receivedDateTime ge 2026-08-31T15:00:00Z and receivedDateTime lt 2026-09-30T15:00:00Z"],
            parameters["$filter"],
        )
        self.assertEqual(["receivedDateTime desc"], parameters["$orderby"])
        self.assertIn("body", parameters["$select"][0].split(","))
        self.assertEqual(["10"], parameters["$top"])
        self.assertEqual(f"Bearer {TOKEN}", options["headers"]["Authorization"])
        self.assertIn('IdType="ImmutableId"', options["headers"]["Prefer"])
        self.assertIn('outlook.body-content-type="text"', options["headers"]["Prefer"])
        self.assertEqual(MAX_PAGE_BYTES, options["max_bytes"])
        self.assertLessEqual(options["timeout"], 15)
        self.assertFalse(result["truncated"])
        self.assertEqual("2026-09-01", result["startDate"])
        self.assertEqual("2026-09-30", result["endDate"])
        self.assertEqual(timezone.utc, datetime.fromisoformat(result["fetchedAt"]).tzinfo)
        item = result["items"][0]
        self.assertEqual("sender@invalid.example", item["fromAddress"])
        self.assertEqual("reader@invalid.example", item["toRecipients"][0]["address"])
        self.assertEqual("가상 본문. 서류를 확인해 주세요.", item["bodyText"])
        self.assertTrue(item["hasAttachments"])
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_sent_items_uses_sent_time_and_same_date_means_one_day(self):
        transport = FakeTransport(response())
        GraphClient(transport).read_messages(
            TOKEN, folder="sentitems", start_date="2026-09-01", end_date="2026-09-01", limit=1,
        )
        url, _ = transport.calls[0]
        parameters = parse_qs(urlsplit(url).query)
        self.assertIn("/sentitems/messages?", url)
        self.assertEqual(
            ["sentDateTime ge 2026-08-31T15:00:00Z and sentDateTime lt 2026-09-01T15:00:00Z"],
            parameters["$filter"],
        )
        self.assertEqual(["sentDateTime desc"], parameters["$orderby"])
        self.assertEqual(["1"], parameters["$top"])

    def test_validation_precedes_auth_and_transport(self):
        invalid = [
            {"folder": "all"}, {"folder": "../messages"}, {"folder": None}, {"folder": []},
            {"start_date": "2026-9-01"}, {"start_date": "2026-02-30"},
            {"start_date": "2026-10-01"}, {"start_date": None}, {"end_date": True},
            {"end_date": "2026-10-02"},
            {"start_date": "0001-01-01", "end_date": "0001-01-01"},
            {"start_date": "9999-12-31", "end_date": "9999-12-31"},
            {"limit": 0}, {"limit": 51}, {"limit": True}, {"limit": 1.0}, {"limit": "10"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                transport = FakeTransport()
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(transport).read_messages(None, **(QUERY | changes))
                self.assertEqual("OUTLOOK_QUERY_INVALID", caught.exception.code)
                self.assertEqual([], transport.calls)

    def test_exactly_31_days_and_leap_day_are_valid(self):
        query = GraphClient.validate_query(folder="inbox", start_date="2024-02-01", end_date="2024-03-02", limit=50)
        self.assertEqual(31, (query.end_utc - query.start_utc).days)
        self.assertEqual("2024-03-02", query.end_date)

    def test_invalid_tokens_never_reach_transport(self):
        for token in (None, "", "abc\r\nInjected: value", "contains space", "한글", "a" * 16_385):
            with self.subTest(token_type=type(token).__name__):
                transport = FakeTransport()
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(transport).read_messages(token, **QUERY)
                self.assertEqual("OUTLOOK_AUTH_REQUIRED", caught.exception.code)
                self.assertEqual([], transport.calls)


class GraphPagingTests(unittest.TestCase):
    def test_opaque_continuation_is_preserved_and_ids_are_deduplicated(self):
        next_url = BASE + "?$skiptoken=opaque%2Bvalue%2F%3D&$top=10"
        transport = FakeTransport(
            response([message("A")], next_url),
            response([message("A"), message("a")]),
        )
        result = GraphClient(transport).read_messages(TOKEN, **QUERY)
        self.assertEqual(["A", "a"], [item["id"] for item in result["items"]])
        self.assertEqual(next_url, transport.calls[1][0])
        self.assertEqual(transport.calls[0][1]["headers"], transport.calls[1][1]["headers"])
        self.assertFalse(result["truncated"])

    def test_empty_pages_can_continue_and_limit_does_not_fetch_extra_page(self):
        transport = FakeTransport(
            response([], BASE + "?$skip=10"),
            response([message("1"), message("2")], BASE + "?$skip=20"),
        )
        result = GraphClient(transport).read_messages(TOKEN, **(QUERY | {"limit": 2}))
        self.assertEqual(2, len(transport.calls))
        self.assertEqual(2, len(result["items"]))
        self.assertTrue(result["truncated"])

    def test_provider_ignoring_top_cannot_exceed_requested_count(self):
        transport = FakeTransport(response([message(str(index)) for index in range(10)]))
        result = GraphClient(transport).read_messages(TOKEN, **(QUERY | {"limit": 2}))
        self.assertEqual(2, len(result["items"]))
        self.assertTrue(result["truncated"])

    def test_exact_count_without_continuation_is_complete(self):
        result = GraphClient(FakeTransport(response([message()]))).read_messages(TOKEN, **(QUERY | {"limit": 1}))
        self.assertFalse(result["truncated"])

    def test_maximum_page_count_returns_explicit_partial_preview(self):
        pages = [response([], BASE + f"?$skip={index + 1}") for index in range(MAX_PAGES)]
        transport = FakeTransport(*pages)
        result = GraphClient(transport).read_messages(TOKEN, **QUERY)
        self.assertEqual(MAX_PAGES, len(transport.calls))
        self.assertEqual([], result["items"])
        self.assertTrue(result["truncated"])

    def test_cycle_is_an_error_instead_of_successful_partial_data(self):
        next_url = BASE + "?$skip=10"
        transport = FakeTransport(response([message()], next_url), response([], next_url))
        with self.assertRaises(OutlookError) as caught:
            GraphClient(transport).read_messages(TOKEN, **QUERY)
        self.assertEqual("OUTLOOK_GRAPH_RESPONSE_INVALID", caught.exception.code)
        self.assertEqual(2, len(transport.calls))

    def test_untrusted_or_out_of_scope_continuations_never_receive_bearer(self):
        links = [
            "https://untrusted.invalid/steal", "http://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
            "https://graph.microsoft.com.evil.invalid/v1.0/me/mailFolders/inbox/messages",
            "https://user@graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
            "https://graph.microsoft.com:444/v1.0/me/mailFolders/inbox/messages",
            "https://graph.microsoft.com:invalid/v1.0/me/mailFolders/inbox/messages",
            "https://graph.microsoft.com/v1.0/users/another-user/messages",
            "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages",
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/../sentitems/messages",
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/%6dessages",
            BASE + "/attachments", BASE + "#fragment", BASE + "?x=\n",
            BASE + "?x=" + "x" * 16_384, "/v1.0/me/messages", 123, "",
        ]
        for next_url in links:
            with self.subTest(next_url=str(next_url)[:100]):
                transport = FakeTransport(response([message()], next_url))
                with self.assertRaises(OutlookError):
                    GraphClient(transport).read_messages(TOKEN, **QUERY)
                self.assertEqual(1, len(transport.calls))

    def test_failed_second_page_does_not_claim_first_page_is_complete(self):
        transport = FakeTransport(response([message()], BASE + "?$skip=10"), response(status=503))
        with self.assertRaises(OutlookError) as caught:
            GraphClient(transport).read_messages(TOKEN, **QUERY)
        self.assertEqual("OUTLOOK_GRAPH_UNAVAILABLE", caught.exception.code)
        self.assertEqual(2, len(transport.calls))


class GraphResponseTests(unittest.TestCase):
    def test_plain_text_and_html_bodies_are_bounded_without_loading_resources(self):
        html = '<head><style>secret style</style></head><div>Hello &amp; goodbye</div><script>fetch("bad")</script><img src="https://untrusted.invalid"><p>본문</p>'
        transport = FakeTransport(response([
            message("plain", body={"contentType": "text", "content": "가" * (MAX_BODY_CHARS + 10)}),
            message("html", body={"contentType": "html", "content": html}),
        ]))
        result = GraphClient(transport).read_messages(TOKEN, **QUERY)
        plain, converted = result["items"]
        self.assertEqual(MAX_BODY_CHARS, len(plain["bodyText"]))
        self.assertTrue(plain["bodyTruncated"])
        self.assertIn("Hello & goodbye", converted["bodyText"])
        self.assertIn("본문", converted["bodyText"])
        self.assertNotIn("secret style", converted["bodyText"])
        self.assertNotIn("fetch", converted["bodyText"])
        self.assertNotIn("untrusted.invalid", converted["bodyText"])
        self.assertFalse(converted["bodyTruncated"])
        self.assertEqual(1, len(transport.calls))

    def test_aggregate_preview_size_is_bounded_and_reports_truncation(self):
        pages = []
        for page_index in range(5):
            entries = [message(str(page_index * 10 + i), body={"contentType": "text", "content": "가" * MAX_BODY_CHARS}) for i in range(10)]
            pages.append(response(entries, BASE + f"?$skip={10 * (page_index + 1)}"))
        result = GraphClient(FakeTransport(*pages)).read_messages(TOKEN, **(QUERY | {"limit": 50}))
        self.assertTrue(result["truncated"])
        self.assertGreater(len(result["items"]), 1)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), MAX_PREVIEW_BYTES)

    def test_missing_optional_fields_and_unicode_surrogates_are_safe(self):
        transport = FakeTransport(response([{"id": "only-id", "subject": "hello\ud800"}]))
        item = GraphClient(transport).read_messages(TOKEN, **QUERY)["items"][0]
        self.assertEqual("", item["bodyText"])
        self.assertFalse(item["hasAttachments"])
        self.assertEqual("hello?", item["subject"])

    def test_malformed_and_oversized_provider_data_is_never_success(self):
        raw_responses = [
            b"not-json", b"null", b"[]", b'{"value":null}', b'{"value":{}}',
            b'{"value":[null]}', b'{"value":[{}]}', b"\xff", b"x" * (MAX_PAGE_BYTES + 1),
        ]
        malformed = [
            message(id=123), message(id=""), message(body=[]),
            message(body={"contentType": "binary", "content": "test"}),
            message(body={"contentType": "text", "content": 3}),
            message(toRecipients="broken"), message(hasAttachments="true"),
            message(**{"from": "broken"}), message(subject=[]),
        ]
        for item in malformed:
            raw_responses.append(response([item]).body)
        for raw in raw_responses:
            with self.subTest(length=len(raw)):
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(FakeTransport(response(raw=raw))).read_messages(TOKEN, **QUERY)
                self.assertEqual("OUTLOOK_GRAPH_RESPONSE_INVALID", caught.exception.code)

    def test_http_errors_are_safe_and_do_not_retry(self):
        expected = {401: "OUTLOOK_AUTH_REQUIRED", 403: "OUTLOOK_PERMISSION_DENIED", 429: "OUTLOOK_THROTTLED", 503: "OUTLOOK_GRAPH_UNAVAILABLE", 302: "OUTLOOK_GRAPH_RESPONSE_INVALID", 404: "OUTLOOK_GRAPH_RESPONSE_INVALID"}
        for status, code in expected.items():
            with self.subTest(status=status):
                transport = FakeTransport(response(status=status, raw=f'{{"error":"{TOKEN}"}}'.encode(), headers={"Retry-After": "25"}))
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(transport).read_messages(TOKEN, **QUERY)
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertEqual(1, len(transport.calls))
                if status == 429:
                    self.assertIn("25초", str(caught.exception))

    def test_exception_and_retry_after_values_cannot_leak_provider_details(self):
        transport = FakeTransport(RuntimeError(TOKEN + " https://private.invalid"))
        with self.assertRaises(OutlookError) as caught:
            GraphClient(transport).read_messages(TOKEN, **QUERY)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn("private.invalid", str(caught.exception))
        transport = FakeTransport(response(status=429, headers={"retry-after": TOKEN}))
        with self.assertRaises(OutlookError) as caught:
            GraphClient(transport).read_messages(TOKEN, **QUERY)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_total_download_cap_and_deadline_fail_closed(self):
        large = response([], BASE + "?$skip=1")
        with patch("ariadne.outlook.graph.MAX_TOTAL_BYTES", 1):
            with self.assertRaises(OutlookError):
                GraphClient(FakeTransport(large)).read_messages(TOKEN, **QUERY)
        transport = FakeTransport()
        with patch("ariadne.outlook.graph.PREVIEW_TIMEOUT", -1):
            with self.assertRaises(OutlookError) as caught:
                GraphClient(transport).read_messages(TOKEN, **QUERY)
        self.assertEqual("OUTLOOK_GRAPH_UNAVAILABLE", caught.exception.code)
        self.assertEqual([], transport.calls)


class GraphTransportLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = []
        outer = cls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/stolen")
                    self.end_headers()
                    return
                if self.path == "/error":
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(TOKEN.encode())
                    return
                payload = b"x" * 1025 if self.path == "/oversize" else b'{"value":[]}'
                self.send_response(200)
                if self.path == "/compressed":
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=5)

    def get(self, path):
        # GraphClient cannot target loopback. This directly exercises only the
        # replaceable HTTP transport, with disposable test data and no secrets.
        return GraphTransport().get(self.origin + path, headers={"Authorization": "Bearer " + TOKEN}, timeout=2, max_bytes=1024)

    def test_transport_returns_success_and_discards_error_bodies(self):
        result = self.get("/ok")
        self.assertEqual(200, result.status)
        self.assertEqual({"value": []}, json.loads(result.body))
        result = self.get("/error")
        self.assertEqual(403, result.status)
        self.assertEqual(b"", result.body)

    def test_redirect_is_not_followed_with_bearer(self):
        result = self.get("/redirect")
        self.assertEqual(302, result.status)
        self.assertNotIn("/stolen", self.paths)

    def test_content_size_and_compression_limits_are_enforced(self):
        for path in ("/oversize", "/compressed"):
            with self.subTest(path=path), self.assertRaises(OutlookError):
                self.get(path)


if __name__ == "__main__":
    unittest.main()
