"""Same-origin web workbench and JSON API for the local pilot."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import re
from urllib.parse import unquote, urlsplit

from . import __version__
from .credentials import CredentialService, CredentialUnavailable
from .models import STATUS_LABELS, ValidationError
from .outlook.errors import OutlookError
from .outlook.service import OutlookService
from .repository import ConflictError, NotFoundError

MAX_BODY_BYTES = 1_048_576
WEB_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/outlook.js": ("outlook.js", "text/javascript; charset=utf-8"),
    "/assets/mail.js": ("mail.js", "text/javascript; charset=utf-8"),
}
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
    "object-src 'none'; frame-ancestors 'none'; form-action 'self'"
)


def _object(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValidationError("JSON object required")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValidationError("Request fields are invalid")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Nonempty text required")
    return value


def _version(value):
    if type(value) is not int or value < 1:
        raise ValidationError("Version must be a positive integer")
    return value


def _ids(value):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValidationError("Evidence IDs must be a list of nonempty strings")
    return value


def _mail_actor(value):
    value = _text(value).strip()
    if len(value) > 200 or value.casefold() in {"ai", "system"}:
        raise ValidationError("Mail capture requires a user actor label of at most 200 characters")
    return value


def create_server(repository, host="127.0.0.1", port=8765, *, credentials=None, outlook=None):
    if host != "127.0.0.1":
        raise ValueError("Local server is restricted to IPv4 loopback")
    credential_service = credentials if credentials is not None else CredentialService()
    outlook_service = outlook if outlook is not None else OutlookService()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"ARIADNE/{__version__}"
        sys_version = ""

        def log_message(self, format, *args):
            # BaseHTTPRequestHandler logs paths and malformed request contents.
            # Operational evidence and request text must not enter debug logs.
            pass

        def _bytes(self, status, payload, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def _reply(self, status, data):
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._bytes(status, payload, "application/json; charset=utf-8")

        def _asset(self, path):
            # Only constants select package resources; request paths never reach
            # the filesystem. In particular, DBs and evidence files are not served.
            name, content_type = WEB_ASSETS[path]
            payload = files("ariadne").joinpath("web", name).read_bytes()
            self._bytes(200, payload, content_type)

        def _error(self, status, code, message):
            self._reply(status, {"error": {"code": code, "message": message}})

        def _check_origin(self):
            own_port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{own_port}", f"localhost:{own_port}"}
            if self.headers.get("Host") not in hosts:
                self._error(403, "HOST_DENIED", "허용되지 않은 호스트입니다.")
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}" for host in hosts}:
                self._error(403, "ORIGIN_DENIED", "같은 출처의 요청만 허용됩니다.")
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._error(403, "ORIGIN_DENIED", "다른 사이트의 요청은 허용되지 않습니다.")
                return False
            return True

        def _body(self):
            if self.headers.get_content_type() != "application/json":
                raise ValidationError("Content-Type must be application/json")
            if self.headers.get("Transfer-Encoding") is not None:
                raise ValidationError("Chunked requests are not supported")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isascii() or not raw_length.isdigit() or len(raw_length) > 8:
                raise ValidationError("Content-Length required")
            length = int(raw_length)
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValidationError("Request exceeds size limit")
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValidationError("Incomplete request")
            try:
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("Duplicate key")
                        result[key] = value
                    return result

                return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeDecodeError) as error:
                raise ValidationError("Invalid JSON") from error

        def _dispatch(self, method):
            if not self._check_origin():
                return
            try:
                parsed = urlsplit(self.path)
                if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
                    raise ValidationError("Unsupported URL")
                path = parsed.path
                if path in WEB_ASSETS:
                    if method == "GET":
                        self._asset(path)
                    else:
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    return
                body = self._body() if method in {"POST", "PATCH"} else None
                if path == "/api/connections/openai":
                    if method == "GET":
                        self._reply(200, credential_service.status())
                    elif method == "POST":
                        b = _object(body, ("apiKey",))
                        self._reply(200, credential_service.save_openai(b["apiKey"]))
                    elif method == "DELETE":
                        self._reply(200, credential_service.delete_openai())
                    else:
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                elif path == "/api/connections/outlook":
                    if method == "GET":
                        self._reply(200, outlook_service.status())
                    elif method == "POST":
                        b = _object(body, ("clientId", "tenantId"))
                        self._reply(200, outlook_service.configure(b["clientId"], b["tenantId"]))
                    elif method == "DELETE":
                        self._reply(200, outlook_service.disconnect())
                    else:
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                elif path == "/api/connections/outlook/login":
                    if method != "POST":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        _object(body, ())
                        self._reply(202, outlook_service.start_login())
                elif path == "/api/connections/outlook/messages":
                    if method != "POST":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        b = _object(body, ("folder", "startDate", "endDate", "limit"))
                        self._reply(200, outlook_service.read_messages(
                            folder=b["folder"], start_date=b["startDate"],
                            end_date=b["endDate"], limit=b["limit"],
                        ))
                elif path == "/api/mail/import":
                    if method != "POST":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        b = _object(body, ("previewId", "messageIds", "actorId"))
                        actor = _mail_actor(b["actorId"])
                        selected = _ids(b["messageIds"])
                        if not 1 <= len(selected) <= 10 or len(set(selected)) != len(selected):
                            raise ValidationError("Select 1 to 10 distinct message IDs")
                        # Only the authenticated Outlook service can supply captured content.
                        # The callback runs after its final connection/generation check.
                        result = outlook_service.import_messages(
                            _text(b["previewId"]), selected,
                            lambda capture: repository.mail.import_messages(capture, actor),
                        )
                        self._reply(200, result)
                elif path == "/api/mail/search":
                    if method != "POST":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        b = _object(body, ("query", "limit", "offset"))
                        self._reply(200, repository.mail.search(b["query"], b["limit"], b["offset"]))
                elif path == "/api/mail":
                    if method == "GET":
                        self._reply(200, repository.mail.search())
                    else:
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                elif match := re.fullmatch(r"/api/mail/([^/]+)/link", path):
                    if method != "POST":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        b = _object(body, ("revision", "workItemId", "expectedVersion", "actorId", "reason"))
                        result = repository.mail.link(
                            unquote(match[1]), _version(b["revision"]), _text(b["workItemId"]),
                            _version(b["expectedVersion"]), _mail_actor(b["actorId"]), _text(b["reason"]),
                        )
                        self._reply(200, result)
                elif match := re.fullmatch(r"/api/mail/([^/]+)(?:/revisions/([1-9][0-9]{0,8}))?", path):
                    if method != "GET":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        revision = int(match[2]) if match[2] is not None else None
                        self._reply(200, {"mail": repository.mail.get(unquote(match[1]), revision)})
                elif method == "GET" and path == "/api/health":
                    self._reply(200, {
                        "version": __version__, "mode": "local-pilot",
                        "graphEnabled": outlook_service.status()["state"] == "connected",
                        "outlookReadImplemented": True, "backgroundSyncEnabled": False,
                        "mailArchiveImplemented": True,
                        "externalAiEnabled": False,
                    })
                elif method == "GET" and path == "/api/statuses":
                    self._reply(200, {"items": [{"id": key, "label": label} for key, label in STATUS_LABELS.items()]})
                elif method == "GET" and path == "/api/funds":
                    self._reply(200, {"items": repository.list_funds()})
                elif method == "POST" and path == "/api/funds":
                    b = _object(body, ("id", "name", "aliases", "actorId"))
                    fund = repository.register_fund(_text(b["id"]), _text(b["name"]), _ids(b["aliases"]), actor_id=_text(b["actorId"]))
                    self._reply(201, {"fund": fund})
                elif path == "/api/work-items" and method == "GET":
                    self._reply(200, {"items": repository.list_work_items()})
                elif path == "/api/work-items" and method == "POST":
                    b = _object(body, ("item", "actorId", "reason"))
                    item = repository.create_work_item(b["item"], _text(b["actorId"]), _text(b["reason"]))
                    self._reply(201, {"item": item})
                elif path == "/api/evidence" and method == "GET":
                    self._reply(200, {"items": repository.list_evidence()})
                elif path == "/api/evidence" and method == "POST":
                    b = _object(body, ("evidence", "actorId"))
                    if isinstance(b["evidence"], dict) and b["evidence"].get("provider") == "outlook":
                        raise ValidationError("Outlook evidence must be captured through the authenticated mail import route")
                    self._reply(201, {"evidence": repository.add_evidence(b["evidence"], _text(b["actorId"]))})
                elif match := re.fullmatch(r"/api/evidence/([^/]+)", path):
                    if method != "GET":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        self._reply(200, {"evidence": repository.get_evidence(unquote(match[1]))})
                elif match := re.fullmatch(r"/api/work-items/([^/]+)(?:/(audit|proposals))?", path):
                    identifier, action = unquote(match[1]), match[2]
                    if method == "GET" and action == "audit":
                        repository.get_work_item(identifier)
                        self._reply(200, {"entries": repository.audit(identifier)})
                    elif method == "GET" and action == "proposals":
                        repository.get_work_item(identifier)
                        self._reply(200, {"items": repository.list_proposals(identifier)})
                    elif method == "POST" and action == "proposals":
                        b = _object(body, ("patch", "baseVersion", "reason"), ("evidenceIds",))
                        proposal = repository.propose(identifier, b["patch"], _version(b["baseVersion"]), reason=_text(b["reason"]), evidence_ids=_ids(b.get("evidenceIds", [])))
                        self._reply(201, {"proposal": proposal})
                    elif method == "GET" and action is None:
                        self._reply(200, {"item": repository.get_work_item(identifier)})
                    elif method == "PATCH" and action is None:
                        b = _object(body, ("patch", "expectedVersion", "actorId", "reason"), ("evidenceIds",))
                        item = repository.update_work_item(identifier, b["patch"], _version(b["expectedVersion"]), _text(b["actorId"]), _text(b["reason"]), _ids(b.get("evidenceIds", [])))
                        self._reply(200, {"item": item})
                    else:
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                elif match := re.fullmatch(r"/api/proposals/([^/]+)/resolve", path):
                    if method != "POST":
                        self._error(405, "METHOD_DENIED", "허용되지 않은 요청 방식입니다.")
                    else:
                        b = _object(body, ("decision", "expectedVersion", "actorId", "reason"))
                        result = repository.resolve_proposal(unquote(match[1]), b["decision"], _version(b["expectedVersion"]), _text(b["actorId"]), _text(b["reason"]))
                        self._reply(200, {"proposal": result})
                else:
                    self._error(404, "NOT_FOUND", "요청한 경로가 없습니다.")
            except ValidationError as error:
                self._error(400, "VALIDATION", str(error))
            except NotFoundError:
                self._error(404, "NOT_FOUND", "자료를 찾을 수 없습니다.")
            except ConflictError as error:
                self._error(409, "CONFLICT", str(error))
            except CredentialUnavailable:
                self._error(503, "CREDENTIAL_STORE_UNAVAILABLE", "보안 저장소를 사용할 수 없습니다. Windows 사용자 계정과 자격증명 저장소를 확인해 주세요.")
            except OutlookError as error:
                self._error(error.status, error.code, error.message)
            except (TimeoutError, ConnectionError):
                self.close_connection = True
            except Exception:
                self._error(500, "INTERNAL_ERROR", "요청 처리에 실패했습니다. 상태를 다시 확인해 주세요.")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_OPTIONS(self):
            self._error(405, "METHOD_DENIED", "다른 출처 연결은 v0.1에서 지원하지 않습니다.")

    return ThreadingHTTPServer((host, port), Handler)
