"""Read-only, bounded Microsoft Graph mailbox previews.

Only the signed-in user's Inbox or Sent Items can be requested. Returned bodies
are plain text, never HTML for rendering. There is no persistence, attachment
download, mailbox mutation, automatic retry, or background collection here.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
import json
import re
import time as clock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import OutlookError


GRAPH_ORIGIN = "https://graph.microsoft.com"
KOREAN_STANDARD_TIME = timezone(timedelta(hours=9), "Asia/Seoul")
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_BODY_CHARS = 20_000
MAX_PAGES = 10
REQUEST_TIMEOUT = 15
PREVIEW_TIMEOUT = 45
SELECT_FIELDS = (
    "id,internetMessageId,subject,from,toRecipients,receivedDateTime,"
    "sentDateTime,body,hasAttachments"
)


def _query_error():
    return OutlookError(
        "OUTLOOK_QUERY_INVALID",
        "받은 편지함 또는 보낸 편지함, YYYY-MM-DD 날짜, 최대 31일, 1~50건을 지정하세요.",
    )


def _response_error():
    return OutlookError(
        "OUTLOOK_GRAPH_RESPONSE_INVALID",
        "Outlook 응답을 안전하게 처리할 수 없습니다. 범위를 줄여 다시 조회하세요.",
        status=502,
    )


@dataclass(frozen=True)
class MailQuery:
    folder: str
    start_date: str
    end_date: str
    limit: int
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class GraphResponse:
    status: int
    headers: dict
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # The bearer token must never follow a Location header, even on Graph.
        return None


class GraphTransport:
    """Small injectable transport; tests never require Microsoft credentials."""

    def __init__(self):
        self._opener = build_opener(_NoRedirect())

    def get(self, url, *, headers, timeout, max_bytes):
        request = Request(url, headers=headers, method="GET")
        deadline = clock.monotonic() + timeout
        try:
            with self._opener.open(request, timeout=timeout) as response:
                # Keep encoded/compressed responses out of the JSON parser. We
                # request identity encoding below and apply an actual read cap.
                content_encoding = response.headers.get("Content-Encoding", "identity")
                if content_encoding.lower() != "identity":
                    raise _response_error()
                chunks = []
                size = 0
                while True:
                    if clock.monotonic() > deadline:
                        raise TimeoutError()
                    chunk = response.read(min(64 * 1024, max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise _response_error()
                    chunks.append(chunk)
                return GraphResponse(response.status, dict(response.headers), b"".join(chunks))
        except HTTPError as error:
            # Provider error bodies may contain confidential details. They are
            # neither read nor forwarded to the UI or application logs.
            try:
                return GraphResponse(error.code, dict(error.headers), b"")
            finally:
                error.close()
        except (OSError, URLError, TimeoutError):
            raise OutlookError(
                "OUTLOOK_GRAPH_UNAVAILABLE",
                "Outlook에 연결하지 못했습니다. 네트워크를 확인한 뒤 다시 조회하세요.",
                status=503,
            ) from None


class _BodyText(HTMLParser):
    """Fallback when a provider sends HTML despite the text preference."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.length = 0
        self.hidden = 0
        self.truncated = False

    def _add(self, value):
        available = MAX_BODY_CHARS - self.length
        self.parts.append(value[:available])
        self.length += min(len(value), available)
        self.truncated = self.truncated or len(value) > available

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr"}:
            self._add("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self._add("\n")

    def handle_data(self, data):
        if not self.hidden:
            self._add(data)


def _text(value, maximum):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _response_error()
    # Lone surrogates are valid JSON escapes but cannot be sent as UTF-8.
    return value[:maximum].encode("utf-8", errors="replace").decode("utf-8")


def _address(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise _response_error()
    email = value.get("emailAddress")
    if email is None:
        email = {}
    if not isinstance(email, dict):
        raise _response_error()
    return {"name": _text(email.get("name"), 500), "address": _text(email.get("address"), 500)}


def _normalize_message(raw):
    if not isinstance(raw, dict):
        raise _response_error()
    message_id = raw.get("id")
    if not isinstance(message_id, str) or not message_id or len(message_id) > 2048:
        raise _response_error()
    sender = _address(raw.get("from"))
    recipients = raw.get("toRecipients")
    if recipients is None:
        recipients = []
    if not isinstance(recipients, list):
        raise _response_error()
    body = raw.get("body")
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise _response_error()
    content = body.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise _response_error()
    content_type = body.get("contentType", "text")
    if not isinstance(content_type, str) or content_type.lower() not in {"text", "html"}:
        raise _response_error()
    if content_type.lower() == "html":
        parser = _BodyText()
        parser.feed(content)
        parser.close()
        body_text = "".join(parser.parts).strip()
        body_truncated = parser.truncated
    else:
        body_text = content[:MAX_BODY_CHARS]
        body_truncated = len(content) > MAX_BODY_CHARS
    attachments = raw.get("hasAttachments", False)
    if not isinstance(attachments, bool):
        raise _response_error()
    return {
        "id": _text(message_id, 2048),
        "internetMessageId": _text(raw.get("internetMessageId"), 2048),
        "subject": _text(raw.get("subject"), 2000),
        "fromName": sender["name"],
        "fromAddress": sender["address"],
        "toRecipients": [_address(recipient) for recipient in recipients[:100]],
        "recipientsTruncated": len(recipients) > 100,
        "receivedAt": _text(raw.get("receivedDateTime"), 64),
        "sentAt": _text(raw.get("sentDateTime"), 64),
        "bodyText": _text(body_text, MAX_BODY_CHARS),
        "bodyTruncated": body_truncated,
        "hasAttachments": attachments,
    }


def _trusted_url(url, folder):
    if not isinstance(url, str) or len(url) > 16_384 or any(ord(c) < 33 for c in url):
        raise _response_error()
    try:
        parsed = urlsplit(url)
        trusted = (
            parsed.scheme == "https"
            and parsed.hostname == "graph.microsoft.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.path.lower() == f"/v1.0/me/mailfolders/{folder}/messages"
        )
    except ValueError:
        raise _response_error() from None
    if not trusted:
        raise _response_error()


def _raise_http_error(status, headers):
    if status == 401:
        raise OutlookError(
            "OUTLOOK_AUTH_REQUIRED", "Outlook 연결이 만료되었습니다. 다시 로그인하세요.", status=401,
        )
    if status == 403:
        raise OutlookError(
            "OUTLOOK_PERMISSION_DENIED",
            "메일 읽기 권한이 없습니다. 조직의 관리자 승인 또는 계정 권한을 확인하세요.",
            status=403,
        )
    if status == 429:
        retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
        message = "Outlook 조회 한도에 도달했습니다. 잠시 후 다시 조회하세요."
        if isinstance(retry, str) and re.fullmatch(r"[0-9]{1,6}", retry):
            seconds = int(retry)
            if 0 < seconds <= 604_800:
                message = f"Outlook 조회 한도에 도달했습니다. {seconds}초 후 다시 조회하세요."
        raise OutlookError("OUTLOOK_THROTTLED", message, status=429)
    if 500 <= status <= 599:
        raise OutlookError(
            "OUTLOOK_GRAPH_UNAVAILABLE", "Outlook 서비스가 일시적으로 응답하지 않습니다. 나중에 다시 조회하세요.",
            status=503,
        )
    raise _response_error()


class GraphClient:
    def __init__(self, transport=None):
        self.transport = transport if transport is not None else GraphTransport()

    @staticmethod
    def validate_query(*, folder, start_date, end_date, limit):
        if folder not in ("inbox", "sentitems") or type(limit) is not int or not 1 <= limit <= 50:
            raise _query_error()
        for value in (start_date, end_date):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise _query_error()
        try:
            first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
            if not 0 <= (last - first).days < 31:
                raise _query_error()
            start_utc = datetime.combine(first, time.min, KOREAN_STANDARD_TIME).astimezone(timezone.utc)
            end_utc = datetime.combine(last + timedelta(days=1), time.min, KOREAN_STANDARD_TIME).astimezone(timezone.utc)
        except (ValueError, OverflowError):
            raise _query_error() from None
        return MailQuery(folder, start_date, end_date, limit, start_utc, end_utc)

    def read_messages(self, token, *, folder, start_date, end_date, limit):
        query = self.validate_query(folder=folder, start_date=start_date, end_date=end_date, limit=limit)
        if not isinstance(token, str) or not token or len(token) > 16_384 or not all(33 <= ord(c) <= 126 for c in token):
            raise OutlookError("OUTLOOK_AUTH_REQUIRED", "Outlook에 다시 로그인하세요.", status=401)
        date_field = "receivedDateTime" if query.folder == "inbox" else "sentDateTime"
        start = query.start_utc.isoformat().replace("+00:00", "Z")
        end = query.end_utc.isoformat().replace("+00:00", "Z")
        parameters = {
            "$filter": f"{date_field} ge {start} and {date_field} lt {end}",
            "$orderby": f"{date_field} desc",
            "$select": SELECT_FIELDS,
            "$top": min(query.limit, 10),
        }
        url = f"{GRAPH_ORIGIN}/v1.0/me/mailFolders/{query.folder}/messages?{urlencode(parameters)}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Prefer": 'outlook.body-content-type="text", IdType="ImmutableId"',
        }
        items, seen_ids, visited = [], set(), set()
        input_bytes = output_bytes = 0
        truncated = False
        deadline = clock.monotonic() + PREVIEW_TIMEOUT
        for page_number in range(MAX_PAGES):
            _trusted_url(url, query.folder)
            if url in visited:
                raise _response_error()
            visited.add(url)
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise OutlookError("OUTLOOK_GRAPH_UNAVAILABLE", "Outlook 조회 시간이 초과되었습니다. 범위를 줄여 다시 조회하세요.", status=503)
            try:
                response = self.transport.get(
                    url, headers=dict(headers), timeout=min(REQUEST_TIMEOUT, remaining), max_bytes=MAX_PAGE_BYTES,
                )
            except OutlookError:
                raise
            except Exception:
                # Injected transports must not leak a URL, bearer token, or
                # provider exception into the browser or server error output.
                raise OutlookError("OUTLOOK_GRAPH_UNAVAILABLE", "Outlook에 연결하지 못했습니다. 다시 조회하세요.", status=503) from None
            if response.status != 200:
                _raise_http_error(response.status, response.headers)
            if not isinstance(response.body, bytes) or len(response.body) > MAX_PAGE_BYTES:
                raise _response_error()
            input_bytes += len(response.body)
            if input_bytes > MAX_TOTAL_BYTES:
                raise _response_error()
            try:
                page = json.loads(response.body)
            except (ValueError, UnicodeError, RecursionError):
                raise _response_error() from None
            if not isinstance(page, dict) or not isinstance(page.get("value"), list):
                raise _response_error()
            next_url = page.get("@odata.nextLink")
            if next_url is not None:
                _trusted_url(next_url, query.folder)
                if next_url in visited:
                    raise _response_error()
            for raw in page["value"]:
                item = _normalize_message(raw)
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                if len(items) >= query.limit:
                    truncated = True
                    break
                size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if output_bytes + size > MAX_PREVIEW_BYTES - 2048:
                    truncated = True
                    break
                output_bytes += size
                items.append(item)
            if truncated or len(items) >= query.limit:
                truncated = truncated or next_url is not None
                break
            if next_url is None:
                break
            if page_number == MAX_PAGES - 1:
                truncated = True
                break
            # Preserve the provider's full continuation query without deriving
            # or incrementing $skip. Authorization and Prefer are reattached.
            url = next_url
        return {
            "items": items,
            "folder": query.folder,
            "startDate": query.start_date,
            "endDate": query.end_date,
            "limit": query.limit,
            "truncated": truncated,
            "fetchedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "message": "일부 메일만 표시했습니다. 기간을 줄여 다시 조회하세요." if truncated else "메일 미리보기입니다. 업무로 저장하거나 자동 수집하지 않습니다.",
        }
