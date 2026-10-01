"""Selected-message capture uses synthetic responses, never company accounts."""

import json
import unittest
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from unittest.mock import patch

from ariadne.outlook.errors import OutlookError
from ariadne.outlook.graph import (
    ATTACHMENT_SELECT_FIELDS, GraphClient, GraphResponse, MAX_ATTACHMENTS,
    MAX_BODY_CHARS, MAX_CAPTURE_BODY_CHARS, MAX_PAGE_BYTES, MAX_PAGES,
)


TOKEN = "synthetic-capture-token"
MESSAGE_ID = "message/With+Opaque=Id"
BASE = "https://graph.microsoft.com/v1.0/me/messages/" + quote(MESSAGE_ID, safe="")


def message(**updates):
    result = {
        "id": MESSAGE_ID, "internetMessageId": "<synthetic@example.invalid>",
        "subject": "가상 자료 요청", "from": {"emailAddress": {"address": "sender@example.invalid"}},
        "toRecipients": [{"emailAddress": {"address": "reader@example.invalid"}}],
        "receivedDateTime": "2026-09-30T23:00:00Z", "sentDateTime": "2026-09-30T22:59:00Z",
        "body": {"contentType": "text", "content": "가상 메일 본문"},
        "hasAttachments": False, "conversationId": "conversation-key", "changeKey": "version-one",
        "lastModifiedDateTime": "2026-09-30T23:00:00Z", "isDraft": False,
    }
    result.update(updates)
    return result


def attachment(attachment_id="attachment-one", **updates):
    result = {
        "id": attachment_id, "name": "가상 문서.xlsx", "contentType": "application/octet-stream",
        "size": 500, "isInline": False, "lastModifiedDateTime": "2026-09-30T23:00:00Z",
        "@odata.type": "#microsoft.graph.fileAttachment",
    }
    result.update(updates)
    return result


def response(value, *, status=200, headers=None):
    return GraphResponse(status, headers or {}, json.dumps(value).encode())


def page(values=None, next_link=None):
    result = {"value": [] if values is None else values}
    if next_link is not None:
        result["@odata.nextLink"] = next_link
    return response(result)


def continuation(**updates):
    parameters = {"$select": ATTACHMENT_SELECT_FIELDS, "$top": "50", "$skiptoken": "opaque+/="}
    parameters.update(updates)
    return BASE + "/attachments?" + urlencode(parameters)


def verification(**updates):
    return response({"id": MESSAGE_ID, "changeKey": "version-one"} | updates)


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **options):
        self.calls.append((url, options))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class MailCaptureGraphTests(unittest.TestCase):
    def test_capture_reads_selected_message_inline_metadata_and_stable_version(self):
        transport = Transport(
            response(message()), page([attachment(isInline=True, contentBytes="must-not-escape")]), verification(),
        )
        result = GraphClient(transport).capture_message(TOKEN, MESSAGE_ID, expected_change_key="version-one")
        self.assertEqual(MESSAGE_ID, result["id"])
        self.assertEqual("conversation-key", result["conversationId"])
        self.assertEqual("version-one", result["changeKey"])
        self.assertFalse(result["hasAttachments"])
        self.assertTrue(result["attachments"][0]["isInline"])
        self.assertEqual("file", result["attachments"][0]["kind"])
        self.assertEqual("not_downloaded", result["attachments"][0]["state"])
        self.assertFalse(result["attachmentsTruncated"])
        self.assertEqual(3, len(transport.calls))
        for index, (url, options) in enumerate(transport.calls):
            parsed = urlsplit(url)
            self.assertEqual(urlsplit(BASE).path + ("/attachments" if index == 1 else ""), parsed.path)
            self.assertEqual(f"Bearer {TOKEN}", options["headers"]["Authorization"])
            self.assertIn('IdType="ImmutableId"', options["headers"]["Prefer"])
            self.assertIn('outlook.body-content-type="text"', options["headers"]["Prefer"])
            self.assertLessEqual(options["timeout"], 15)
            self.assertEqual(MAX_PAGE_BYTES, options["max_bytes"])
            self.assertNotIn("$expand", parse_qs(parsed.query))
            self.assertNotIn("/$value", url)
            self.assertNotIn("contentBytes", url)
        attachment_query = parse_qs(urlsplit(transport.calls[1][0]).query)
        self.assertEqual([ATTACHMENT_SELECT_FIELDS], attachment_query["$select"])
        self.assertEqual(["id,changeKey"], parse_qs(urlsplit(transport.calls[-1][0]).query)["$select"])
        self.assertNotIn("must-not-escape", json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_capture_body_is_larger_than_preview_and_explicitly_bounded(self):
        raw = message(body={"contentType": "text", "content": "가" * (MAX_CAPTURE_BODY_CHARS + 10)})
        captured = GraphClient(Transport(response(raw), page(), verification())).capture_message(TOKEN, MESSAGE_ID)
        preview = GraphClient(Transport(page([raw]))).read_messages(
            TOKEN, folder="inbox", start_date="2026-09-30", end_date="2026-09-30", limit=1,
        )["items"][0]
        self.assertEqual(MAX_CAPTURE_BODY_CHARS, len(captured["bodyText"]))
        self.assertTrue(captured["bodyTruncated"])
        self.assertEqual(MAX_BODY_CHARS, len(preview["bodyText"]))
        self.assertEqual("version-one", preview["changeKey"])

    def test_html_fallback_and_recipient_truncation_are_explicit(self):
        raw = message(
            body={"contentType": "html", "content": "<script>bad()</script><p>" + "x" * (MAX_CAPTURE_BODY_CHARS + 1) + "</p>"},
            toRecipients=[{"emailAddress": {"address": f"a{i}@example.invalid"}} for i in range(101)],
        )
        result = GraphClient(Transport(response(raw), page(), verification())).capture_message(TOKEN, MESSAGE_ID)
        self.assertTrue(result["bodyTruncated"])
        self.assertLessEqual(len(result["bodyText"]), MAX_CAPTURE_BODY_CHARS)
        self.assertNotIn("bad()", result["bodyText"])
        self.assertEqual(100, len(result["toRecipients"]))
        self.assertTrue(result["recipientsTruncated"])

    def test_opaque_pagination_preserves_metadata_selection_and_deduplicates(self):
        next_link = continuation()
        transport = Transport(
            response(message()), page([attachment("A")], next_link),
            page([attachment("A"), attachment("a", **{"@odata.type": "#microsoft.graph.itemAttachment"}),
                  attachment("reference", **{"@odata.type": "#microsoft.graph.referenceAttachment"}),
                  attachment("unknown", **{"@odata.type": "#custom.attachment"})]), verification(),
        )
        result = GraphClient(transport).capture_message(TOKEN, MESSAGE_ID)
        self.assertEqual(next_link, transport.calls[2][0])
        self.assertEqual(["A", "a", "reference", "unknown"], [item["id"] for item in result["attachments"]])
        self.assertEqual(["file", "item", "reference", "unknown"], [item["kind"] for item in result["attachments"]])
        self.assertFalse(result["attachmentsTruncated"])

    def test_attachment_count_cap_and_page_cap_report_incomplete_lists(self):
        for amount, has_next, expected in ((100, False, False), (100, True, True), (101, False, True)):
            with self.subTest(amount=amount, has_next=has_next):
                transport = Transport(
                    response(message()),
                    page([attachment(str(i)) for i in range(amount)], continuation() if has_next else None), verification(),
                )
                result = GraphClient(transport).capture_message(TOKEN, MESSAGE_ID)
                self.assertEqual(MAX_ATTACHMENTS, len(result["attachments"]))
                self.assertEqual(expected, result["attachmentsTruncated"])
                self.assertEqual(3, len(transport.calls))
        transport = Transport(response(message()), *[
            page([], continuation(**{"$skiptoken": str(i)})) for i in range(MAX_PAGES)
        ], verification())
        result = GraphClient(transport).capture_message(TOKEN, MESSAGE_ID)
        self.assertTrue(result["attachmentsTruncated"])
        self.assertEqual(MAX_PAGES + 2, len(transport.calls))

    def test_changed_preview_or_mid_capture_version_rejects_whole_message(self):
        transport = Transport(response(message(changeKey="new-version")))
        with self.assertRaises(OutlookError) as caught:
            GraphClient(transport).capture_message(TOKEN, MESSAGE_ID, expected_change_key="version-one")
        self.assertEqual(("OUTLOOK_MESSAGE_CHANGED", 409), (caught.exception.code, caught.exception.status))
        self.assertEqual(1, len(transport.calls))
        transport = Transport(response(message()), page([attachment()]), verification(changeKey="new-version"))
        with self.assertRaises(OutlookError) as caught:
            GraphClient(transport).capture_message(TOKEN, MESSAGE_ID)
        self.assertEqual("OUTLOOK_MESSAGE_CHANGED", caught.exception.code)

    def test_selected_ids_and_keys_cannot_change_resource_or_headers(self):
        invalid = [None, "", True, 123, [], "a" * 2049, "../me", ".", "a?x=y", "x#frag", "x%2Fme", "x\r\n", "https://bad.invalid", "a\\b", "한글", "\ud800"]
        for value in invalid:
            with self.subTest(value=repr(value)[:30]):
                transport = Transport()
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(transport).capture_message(TOKEN, value)
                self.assertEqual("OUTLOOK_MESSAGE_INVALID", caught.exception.code)
                self.assertEqual([], transport.calls)
                if value is not None:
                    with self.assertRaises(OutlookError):
                        GraphClient(transport).capture_message(TOKEN, MESSAGE_ID, expected_change_key=value)
                    self.assertEqual([], transport.calls)

    def test_ids_and_keys_are_never_truncated_or_substituted(self):
        for raw in [message(id="another-id"), message(id="a" * 2049), message(changeKey=""),
                    message(changeKey="a" * 2049), message(conversationId="a" * 2049), message(isDraft="false")]:
            with self.subTest(raw_field=list(raw)[-1]):
                transport = Transport(response(raw))
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(transport).capture_message(TOKEN, MESSAGE_ID)
                self.assertEqual("OUTLOOK_GRAPH_RESPONSE_INVALID", caught.exception.code)
                self.assertEqual(1, len(transport.calls))
        for final in [verification(id="different-id"), verification(changeKey="")]:
            with self.assertRaises(OutlookError):
                GraphClient(Transport(response(message()), page(), final)).capture_message(TOKEN, MESSAGE_ID)

    def test_untrusted_continuations_never_receive_tokens_or_expand_content(self):
        links = [
            continuation().replace("graph.microsoft.com", "attacker.invalid"),
            continuation().replace("https:", "http:"),
            continuation().replace("graph.microsoft.com", "graph.microsoft.com@attacker.invalid"),
            continuation().replace("graph.microsoft.com", "graph.microsoft.com:444"),
            continuation().replace("/attachments", "/attachments/$value"),
            continuation().replace(quote(MESSAGE_ID, safe=""), "different-message"),
            continuation().replace("/attachments", "/Attachments"),
            continuation(**{"$expand": "microsoft.graph.itemAttachment/item"}),
            continuation(**{"$select": "id,contentBytes"}),
            BASE + "/attachments?$skiptoken=missing-select",
            continuation() + "&%24select=" + quote(ATTACHMENT_SELECT_FIELDS),
            continuation() + "#fragment", continuation() + "\n", 100, "",
        ]
        for link in links:
            with self.subTest(link=str(link)[:80]):
                transport = Transport(response(message()), page([attachment()], link))
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(transport).capture_message(TOKEN, MESSAGE_ID)
                self.assertEqual("OUTLOOK_GRAPH_RESPONSE_INVALID", caught.exception.code)
                self.assertEqual(2, len(transport.calls))

    def test_repeated_continuation_fails_without_silent_partial_success(self):
        link = continuation()
        transport = Transport(response(message()), page([], link), page([], link))
        with self.assertRaises(OutlookError) as caught:
            GraphClient(transport).capture_message(TOKEN, MESSAGE_ID)
        self.assertEqual("OUTLOOK_GRAPH_RESPONSE_INVALID", caught.exception.code)
        self.assertEqual(3, len(transport.calls))

    def test_failed_attachment_page_or_final_verification_rejects_whole_message(self):
        for last in (response({}, status=503), response({}, status=401), response({}, status=403),
                     response({}, status=429), response({}, status=302), response({}, status=404)):
            with self.subTest(status=last.status):
                for responses in ([response(message()), page([attachment()], continuation()), last],
                                  [response(message()), page([attachment()]), last]):
                    with self.assertRaises(OutlookError):
                        GraphClient(Transport(*responses)).capture_message(TOKEN, MESSAGE_ID)
        with self.assertRaises(OutlookError) as caught:
            GraphClient(Transport(response({}, status=404))).capture_message(TOKEN, MESSAGE_ID)
        self.assertEqual(("OUTLOOK_MESSAGE_NOT_FOUND", 404), (caught.exception.code, caught.exception.status))

    def test_malformed_metadata_never_becomes_a_saved_complete_record(self):
        invalid = [None, [], attachment(id=""), attachment(id="a" * 2049), attachment(size=True),
                   attachment(size=-1), attachment(size=2**63), attachment(size="5"),
                   attachment(isInline="false"), attachment(name=[]), attachment(lastModifiedDateTime="2026-10-01"),
                   attachment(**{"@odata.type": []})]
        for entry in invalid:
            with self.subTest(entry=repr(entry)[:100]):
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(Transport(response(message()), page([entry]))).capture_message(TOKEN, MESSAGE_ID)
                self.assertEqual("OUTLOOK_GRAPH_RESPONSE_INVALID", caught.exception.code)

    def test_missing_optional_metadata_has_empty_values_and_known_state(self):
        raw = {"id": MESSAGE_ID, "changeKey": "version-one"}
        result = GraphClient(Transport(response(raw), page([{"id": "attachment"}]), verification())).capture_message(TOKEN, MESSAGE_ID)
        self.assertEqual("", result["conversationId"])
        self.assertEqual("", result["lastModifiedAt"])
        self.assertFalse(result["isDraft"])
        self.assertEqual({"id": "attachment", "name": "", "contentType": "", "size": 0,
                          "isInline": False, "kind": "unknown", "lastModifiedAt": "", "state": "not_downloaded"},
                         result["attachments"][0])

    def test_capture_rejects_invalid_tokens_before_transport(self):
        for token in (None, "", "a\r\nAuthorization: bad", "한글", "x" * 16_385):
            transport = Transport()
            with self.assertRaises(OutlookError) as caught:
                GraphClient(transport).capture_message(token, MESSAGE_ID)
            self.assertEqual("OUTLOOK_AUTH_REQUIRED", caught.exception.code)
            self.assertEqual([], transport.calls)

    def test_deadline_is_shared_bounded_and_checked_after_transport(self):
        for deadline in (1, float("inf"), float("nan"), True, "later"):
            transport = Transport()
            with patch("ariadne.outlook.graph.clock.monotonic", return_value=10):
                with self.assertRaises(OutlookError) as caught:
                    GraphClient(transport).capture_message(TOKEN, MESSAGE_ID, deadline=deadline)
            self.assertEqual("OUTLOOK_GRAPH_UNAVAILABLE", caught.exception.code)
            self.assertEqual([], transport.calls)
        transport = Transport(response(message()))
        with patch("ariadne.outlook.graph.clock.monotonic", side_effect=[10, 10, 12]):
            with self.assertRaises(OutlookError):
                GraphClient(transport).capture_message(TOKEN, MESSAGE_ID, deadline=11)
        self.assertEqual(1, transport.calls[0][1]["timeout"])

    def test_response_and_total_size_limits_are_enforced(self):
        oversized = GraphResponse(200, {}, b"x" * (MAX_PAGE_BYTES + 1))
        with self.assertRaises(OutlookError):
            GraphClient(Transport(oversized)).capture_message(TOKEN, MESSAGE_ID)
        with patch("ariadne.outlook.graph.MAX_TOTAL_BYTES", 10):
            with self.assertRaises(OutlookError):
                GraphClient(Transport(response(message()))).capture_message(TOKEN, MESSAGE_ID)

    def test_provider_transport_exception_does_not_expose_token_or_response(self):
        with self.assertRaises(OutlookError) as caught:
            GraphClient(Transport(RuntimeError(TOKEN))).capture_message(TOKEN, MESSAGE_ID)
        self.assertEqual("OUTLOOK_GRAPH_UNAVAILABLE", caught.exception.code)
        self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
