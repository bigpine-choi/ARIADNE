"""Loopback-only JSON API for the v0.1 local pilot. No external actions."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, quote

from . import __version__
from .credentials import CredentialError
from .models import STATUS_LABELS, ValidationError
from .repository import ConflictError, NotFoundError

MAX_BODY_BYTES = 1_048_576


def _public_native_diagnostics(value, *, outlook=False):
    """Publish fixed preflight fields only, never transport output or identifiers."""
    from .native_powershell import POLICIES, SCOPES

    allowed_codes = {
        'WINDOWS_REQUIRED', 'POWERSHELL_UNAVAILABLE',
        'POWERSHELL_DIAGNOSTICS_UNAVAILABLE', 'POWERSHELL_POLICY_BLOCKED',
        'POWERSHELL_SCRIPT_INVALID', 'CLASSIC_OUTLOOK_MISSING',
        'CLASSIC_OUTLOOK_UNVERIFIED',
    }
    value = value if isinstance(value, dict) else {}
    raw_codes = value.get('codes')
    if (not isinstance(raw_codes, list)
            or any(not isinstance(code, str) or code not in allowed_codes for code in raw_codes)):
        codes = ['POWERSHELL_DIAGNOSTICS_UNAVAILABLE']
    else:
        codes = list(dict.fromkeys(raw_codes))
    def policy(name):
        raw = value.get(name)
        return raw if isinstance(raw, str) and raw in POLICIES else None
    version = value.get('version')
    version = version if isinstance(version, str) and re.fullmatch(r'5\.1\.\d+\.\d+', version) else None
    raw_scopes = value.get('scopes')
    raw_scopes = raw_scopes if isinstance(raw_scopes, dict) else {}
    scopes = {scope: raw_scopes.get(scope) if isinstance(raw_scopes.get(scope), str)
              and raw_scopes[scope] in POLICIES else None for scope in SCOPES}
    registered = value.get('classicOutlookRegistered')
    registered = registered if type(registered) is bool else None
    ready = (value.get('ready') is True and not codes and version is not None
             and policy('effectivePolicy') is not None and all(scopes.values())
             and (not outlook or registered is True))
    if not ready and not codes:
        codes = ['POWERSHELL_DIAGNOSTICS_UNAVAILABLE']
    result = {
        'ready': bool(ready), 'host': 'windows_powershell_5_1',
        'version': version, 'effectivePolicy': policy('effectivePolicy'),
        'scopes': scopes, 'groupPolicy': policy('groupPolicy'),
        'processPolicyRequested': 'RemoteSigned' if value.get('processPolicyRequested') == 'RemoteSigned' else None,
        'persistentPolicyChanged': False, 'accountVerified': False, 'codes': codes,
    }
    if outlook:
        result['classicOutlookRegistered'] = registered
    return result


def _native_diagnostics():
    from . import local_mail, teams_uia
    from .native_powershell import diagnostics

    # Registry and the actual collector host policy only. No COM activation,
    # credentials, workspace settings, source reads or collection operations.
    return {
        'mail': _public_native_diagnostics(diagnostics(local_mail.SCRIPT, require_outlook=True), outlook=True),
        'teams': _public_native_diagnostics(diagnostics(Path(teams_uia.__file__).parent / 'windows/teams.ps1')),
    }


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


def _validation_id(value):
    if (not isinstance(value, str) or "-validation-" not in value
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,119}", value)):
        raise ValidationError("운영 검증 실행번호를 확인하세요.")
    return value


def _validation_runs(store, settings, identifier=None):
    """Restore only this owner's validation runs with intact pinned reports."""
    from .weekly_worker import VALIDATION_ID, DIGEST, _public

    owner = settings.get('mailbox', '').strip().casefold()
    if store is None or not owner or settings.get('device_role') == 'company-relay':
        return []
    owner_key = hashlib.sha256(owner.encode()).hexdigest()[:12]
    items = []
    for row in store.runs():
        try:
            run = dict(row)
            if identifier is not None and run.get('id') != identifier:
                continue
            if (not isinstance(run.get('id'), str) or not VALIDATION_ID.fullmatch(run['id'])
                    or run.get('status') not in {'prepared', 'sending', 'submitted', 'delivery_unknown', 'delivered'}):
                continue
            detail = json.loads(run['detail'])
            if (not isinstance(detail, dict) or detail.get('recipient') != owner
                    or detail.get('kind') != 'operational_validation'):
                continue
            end = datetime.fromisoformat(run['end'])
            start = datetime.fromisoformat(run['start'])
            if (start >= end or run['id'].split('-validation-', 1)[0]
                    != end.strftime('%Y-%m-%d') + '-' + owner_key):
                continue
            digest = detail.get('outputHash')
            version = detail.get('outputVersionId')
            if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                    or type(version) is not int or version < 1):
                continue
            if hashlib.sha256(store.read_version(version)).hexdigest() != digest:
                continue
            run['detail'] = detail
            items.append(_public(run))
            if len(items) >= 50:
                break
        except Exception:
            # Malformed or changed records must not leak their private contents.
            continue
    return items


def create_server(repository, host="127.0.0.1", port=8765, *, automation_store=None, automation_settings=None,
                  credential_store=None, draft_store=None, workspace=None, calendar_store=None):
    if host != "127.0.0.1":
        raise ValueError("v0.1 server is restricted to IPv4 loopback")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ARIADNE/0.1"
        sys_version = ""

        def log_message(self, format, *args):
            # BaseHTTPRequestHandler logs paths and malformed request contents.
            # Operational evidence and request text must not enter debug logs.
            pass

        def _reply(self, status, data):
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status, code, message):
            self._reply(status, {"error": {"code": code, "message": message}})

        def _validation_reply(self, callback, *args, **kwargs):
            # These operations may access credentials, mailbox and filesystem.
            # Their exception text must never be returned to the browser.
            try:
                result = callback(*args, **kwargs)
            except NotFoundError:
                self._error(404, "NOT_FOUND", "운영 검증 자료를 찾을 수 없습니다.")
            except ConflictError:
                self._error(409, "CONFLICT", "보고서 또는 발송 상태가 변경됐습니다. 검증 결과를 다시 확인하세요.")
            except (CredentialError, ValidationError, ValueError):
                self._error(400, "VALIDATION", "운영 검증 조건을 확인하세요. 연결 상태와 보고서 검토 결과를 확인한 뒤 다시 시도하세요.")
            except Exception:
                self._error(500, "INTERNAL_ERROR", "운영 검증 처리에 실패했습니다. 발송 결과가 불명확하면 재발송하지 말고 메일 도착을 확인하세요.")
            else:
                self._reply(200, result)

        def _check_origin(self):
            own_port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{own_port}", f"localhost:{own_port}"}
            if self.headers.get("Host") not in hosts:
                self._discard_rejected_body()
                self._error(403, "HOST_DENIED", "허용되지 않은 호스트입니다.")
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}" for host in hosts}:
                self._discard_rejected_body()
                self._error(403, "ORIGIN_DENIED", "같은 출처의 요청만 허용됩니다.")
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._discard_rejected_body()
                self._error(403, "ORIGIN_DENIED", "다른 사이트의 요청은 허용되지 않습니다.")
                return False
            return True

        def _discard_rejected_body(self):
            # Closing before an ordinary POST body arrives can reset the socket
            # on Windows and hide the 403 response. Discard only a bounded body;
            # a denied request is never parsed or dispatched.
            length = self.headers.get("Content-Length", "")
            if (self.headers.get("Transfer-Encoding") is not None
                    or not length.isascii() or not length.isdigit() or len(length) > 8):
                return
            length = int(length)
            if not 0 < length <= MAX_BODY_BYTES:
                return
            try:
                self.connection.settimeout(1)
                self.rfile.read(length)
            except (TimeoutError, ConnectionError, OSError):
                pass

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
                body = self._body() if method in {"POST", "PATCH"} else None
                if method == "GET" and path == "/":
                    payload = (Path(__file__).parent / 'web' / 'index.html').read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(payload)
                elif method == 'GET' and path in {'/automation.js','/automation.css','/hiworks.js','/navigation.js','/workspace.js','/calendar.js'}:
                    filename = path.removeprefix('/')
                    payload = (Path(__file__).parent / 'web' / filename).read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/javascript; charset=utf-8' if filename.endswith('.js') else 'text/css; charset=utf-8')
                    self.send_header('Content-Length',str(len(payload)))
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.end_headers()
                    self.wfile.write(payload)
                elif method == 'GET' and path == '/api/workspace/native-diagnostics':
                    self._reply(200, _native_diagnostics())
                elif path.startswith('/api/workspace/'):
                    if workspace is None:raise ValidationError('통합 서비스를 연결하세요.')
                    if method=='GET' and path=='/api/workspace/validation/runs':
                        self._validation_reply(lambda: {'items': _validation_runs(automation_store, workspace.settings())})
                    elif method=='GET' and (match:=re.fullmatch(r'/api/workspace/validation/runs/([A-Za-z0-9-]+)',path)):
                        identifier=_validation_id(match[1])
                        def validation_detail():
                            items=_validation_runs(automation_store,workspace.settings(),identifier)
                            if not items:raise NotFoundError('Validation run not found')
                            return {'run':items[0]}
                        self._validation_reply(validation_detail)
                    elif method=='GET' and path=='/api/workspace/status':
                        from .weekly_archive import archive_runs
                        from .handoff import status as handoff_status
                        from .desktop import schedule_status
                        from .cloud_handoff import status as cloud_status
                        self._reply(200,{'settings':workspace.settings(),'jobs':workspace.jobs(),
                          'schedule':schedule_status(workspace.settings()['device_role']),
                          'archiveRuns':archive_runs(automation_store),
                          'handoff':{**handoff_status(automation_store),'keySaved':(workspace.settings_path.parent/'credentials/shared-folder.dpapi').exists()},
                          'cloud':cloud_status(automation_store,workspace.settings_path.parent),
                          'browser':workspace.bridge.status() if workspace.bridge else {'paired':False,'lastSeen':None}})
                    elif method=='POST' and path=='/api/workspace/handoff-web-connect':
                        from .cloud_handoff import open_connection
                        _object(body,())
                        settings=workspace.settings()
                        if settings['device_role'] not in {'company-relay','omen'} or settings['handoff_transport']!='web':
                            raise ValidationError('회사 PC의 웹 OneDrive 전달 설정을 먼저 저장하세요.')
                        self._reply(202,open_connection(workspace.settings_path.parent,settings['handoff_web_url']))
                    elif method=='POST' and path=='/api/workspace/schedule-register':
                        from .desktop import register_schedule,schedule_status
                        _object(body,())
                        role=workspace.settings()['device_role']
                        try:register_schedule(workspace.settings_path.parent,role)
                        except (ValueError,CredentialError) as error:raise ValidationError(str(error)) from None
                        self._reply(200,schedule_status(role))
                    elif method=='POST' and path=='/api/workspace/handoff-pair':
                        from .handoff import install_key,read_key
                        import base64,os
                        _object(body,(),())
                        if workspace.settings()['device_role']!='company-relay':raise ValidationError('자료 연결 코드는 회사 PC에서 발급합니다.')
                        directory=workspace.settings_path.parent
                        if not (directory/'credentials/shared-folder.dpapi').exists():install_key(directory,os.urandom(32))
                        self._reply(200,{'connectionCode':base64.urlsafe_b64encode(read_key(directory)).decode('ascii')})
                    elif method=='POST' and path=='/api/workspace/handoff-key':
                        from .handoff import install_key
                        import base64
                        b=_object(body,('connectionCode',),())
                        if workspace.settings()['device_role']!='omen':raise ValidationError('회사 자료 연결은 오멘에서 등록합니다.')
                        value=_text(b['connectionCode'])
                        if not re.fullmatch(r'[A-Za-z0-9_-]{43}=',value):raise ValidationError('자료 연결 코드 형식을 확인하세요.')
                        install_key(workspace.settings_path.parent,base64.urlsafe_b64decode(value))
                        self._reply(200,{'saved':True})
                    elif method=='POST' and path=='/api/workspace/settings':
                        self._reply(200,workspace.save_settings(body))
                    elif method=='POST' and path=='/api/workspace/start':
                        b=_object(body,('action',));self._reply(202,workspace.start(b['action']))
                    elif method=='POST' and path=='/api/workspace/validation/prepare':
                        b=_object(body,('scopeAcknowledged',))
                        if type(b['scopeAcknowledged']) is not bool:
                            raise ValidationError('수집 범위 확인값은 참 또는 거짓이어야 합니다.')
                        self._validation_reply(workspace.prepare_validation, scope_acknowledged=b['scopeAcknowledged'])
                    elif method=='POST' and path=='/api/workspace/validation/send':
                        b=_object(body,('id','outputHash','approval'))
                        identifier=_validation_id(b['id'])
                        if b['approval'] is not True:
                            raise ValidationError('검토한 보고서의 발송 승인이 필요합니다.')
                        if not isinstance(b['outputHash'],str) or not re.fullmatch(r'[0-9a-f]{64}',b['outputHash']):
                            raise ValidationError('검토한 보고서의 파일 확인값을 확인하세요.')
                        self._validation_reply(workspace.send_validation,identifier,b['outputHash'])
                    elif method=='POST' and path=='/api/workspace/validation/delivery':
                        b=_object(body,('id',))
                        self._validation_reply(workspace.confirm_validation,_validation_id(b['id']))
                    elif method=='GET' and (match:=re.fullmatch(r'/api/workspace/messages/(\d+)',path)):
                        self._reply(200,workspace.messages(int(match[1])))
                    elif method=='GET' and (match:=re.fullmatch(r'/api/workspace/messages/(outlook|teams|hiworks)/(\d+)',path)):
                        self._reply(200,workspace.messages(int(match[2]),kind=match[1]))
                    elif method=='POST' and path=='/api/workspace/evidence':
                        b=_object(body,('versionId',))
                        if type(b['versionId']) is not int:raise ValidationError('보관 버전을 확인하세요.')
                        self._reply(201,{'evidence':workspace.evidence(b['versionId'])})
                    elif method=='POST' and path=='/api/workspace/browser-pair':
                        _object(body,());self._reply(200,{'connectionCode':workspace.bridge.token(create=True)})
                    else:self._error(404,'NOT_FOUND','요청한 경로가 없습니다.')
                elif path.startswith('/api/calendar/'):
                    if calendar_store is None:raise ValidationError('캘린더 저장소를 연결하세요.')
                    if method=='GET' and (match:=re.fullmatch(r'/api/calendar/months/(20\d{2}-\d{2})',path)):
                        self._reply(200,calendar_store.view(match[1]))
                    elif method=='POST' and path=='/api/calendar/import':
                        self._reply(200,calendar_store.import_snapshot(body))
                    elif method=='POST' and path=='/api/calendar/review':
                        self._reply(200,calendar_store.review(body))
                    elif method=='POST' and path=='/api/calendar/propose':
                        from .calendar_ai import propose
                        b=_object(body,('month','sourceIds'))
                        self._reply(201,propose(calendar_store,b['month'],b['sourceIds'],workspace.settings()['model'],credential_store=credential_store))
                    else:self._error(404,'NOT_FOUND','요청한 경로가 없습니다.')
                elif path.startswith("/api/settings/openai"):
                    if credential_store is None:
                        self._error(503, "CREDENTIALS_DISABLED", "키 저장소가 연결되지 않았습니다.")
                    elif method == "GET" and path == "/api/settings/openai":
                        self._reply(200, credential_store.status())
                    elif method == "POST" and path == "/api/settings/openai":
                        b = _object(body, ("apiKey",))
                        self._reply(200, credential_store.save(b["apiKey"]))
                    elif method == "POST" and path == "/api/settings/openai/check":
                        from .openai_connection import check_connection, validate_model
                        b = _object(body, ("model",))
                        try:
                            model = validate_model(b["model"])
                        except ValueError:
                            raise ValidationError("사용할 OpenAI 모델명을 입력하세요. 예: gpt-5-mini") from None
                        if workspace is None:
                            raise ValidationError("모델 설정을 저장할 업무 환경이 연결되지 않았습니다.")
                        with workspace.lock:
                            settings = workspace.settings()
                            settings["model"] = model
                            workspace.save_settings(settings)
                        self._reply(200, check_connection(model, credential_store))
                    elif method == "POST" and path == "/api/settings/openai/remove":
                        _object(body, ())
                        self._reply(200, credential_store.remove())
                    else:
                        self._error(404, "NOT_FOUND", "요청한 경로가 없습니다.")
                elif path.startswith("/api/hiworks/"):
                    if draft_store is None:
                        self._error(503, "DRAFTS_DISABLED", "초안 저장소가 연결되지 않았습니다.")
                    elif method == "GET" and path == "/api/hiworks/drafts":
                        self._reply(200, {"items": draft_store.list()})
                    elif method == "POST" and path == "/api/hiworks/drafts":
                        from .hiworks import prepare_approval
                        draft = prepare_approval(body, {x["id"] for x in repository.list_evidence()})
                        self._reply(201, {"draft": draft_store.save(draft)})
                    elif method == "GET" and (match := re.fullmatch(r"/api/hiworks/drafts/([0-9a-f]{64})", path)):
                        self._reply(200, {"draft": draft_store.get(match[1])})
                    else:
                        self._error(404, "NOT_FOUND", "요청한 경로가 없습니다.")
                elif path.startswith('/api/automation/'):
                    if workspace is not None: automation_config=workspace.settings()
                    else: automation_config=automation_settings
                    if automation_store is None or automation_config is None:
                        self._error(503,'AUTOMATION_DISABLED','자동화 설정을 연결한 뒤 실행하세요.')
                    elif method == 'GET' and path == '/api/automation/status':
                        self._reply(200, {'folderConfigured':bool(automation_config['folder']),
                          'mailboxConfigured':bool(automation_config['mailbox']),
                          'enabled':automation_config['enabled'],'runs':automation_store.runs(),
                          'teamsMode':'ui_automation_no_input','teamsSchedule':'weekdays_1200_KST_once_no_catchup'})
                    elif method == 'POST' and path == '/api/automation/sync':
                        _object(body,())
                        if not automation_config['folder']:
                            raise ValidationError('수집 폴더를 먼저 설정하세요.')
                        if workspace is not None:self._reply(202,workspace.start('folder'))
                        else:self._reply(200,automation_store.sync_folder(automation_config['folder']))
                    elif method == 'GET' and path == '/api/automation/versions':
                        from .automation_store import canonical
                        self._reply(200,{'items':automation_store.history('folder:'+canonical(automation_config['folder']))})
                    elif method == 'GET' and (match := re.fullmatch(r'/api/automation/versions/(\d+)/download',path)):
                        identifier=int(match[1])
                        from .automation_store import canonical
                        allowed={r['id']:r for r in automation_store.history('folder:'+canonical(automation_config['folder']))}
                        if identifier not in allowed:
                            raise NotFoundError('Version not found')
                        payload=automation_store.read_version(identifier)
                        self.send_response(200)
                        self.send_header('Content-Type','application/octet-stream')
                        filename=Path(allowed[identifier]['source_id']).name
                        self.send_header('Content-Disposition',f'attachment; filename="ariadne-version-{identifier}{Path(filename).suffix}"; filename*=UTF-8\'\'{quote(filename,safe="")}')
                        self.send_header('Content-Length',str(len(payload)))
                        self.send_header('Cache-Control','no-store')
                        self.send_header('X-Content-Type-Options','nosniff')
                        self.end_headers()
                        self.wfile.write(payload)
                    else:
                        self._error(404,'NOT_FOUND','요청한 경로가 없습니다.')
                elif method=='GET' and (match:=re.fullmatch(r'/api/reports/([A-Za-z0-9-]+)(?:/versions/(\d+))?/download',path)):
                    if automation_store is None:raise NotFoundError('Report not found')
                    run=next((r for r in automation_store.runs() if r['id']==match[1]),None)
                    identifier=json.loads(run['detail']).get('outputVersionId') if run else None
                    if identifier is None:raise NotFoundError('Report not found')
                    if match[2] is not None and int(match[2]) != identifier:
                        raise NotFoundError('Report version not found')
                    payload=automation_store.read_version(identifier)
                    self.send_response(200);self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    self.send_header('Content-Disposition','attachment; filename="weekly-'+match[1]+'.xlsx"')
                    self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(payload)
                elif method == "GET" and path == "/api/health":
                    from .desktop import location_id
                    from .config import data_directory
                    self._reply(200, {"version": __version__, "mode": "local-pilot", "graphEnabled": False, "externalAiEnabled": False,
                        'dataLocationId':location_id(data_directory())})
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
            except CredentialError as error:
                self._error(400, "CREDENTIALS", str(error))
            except ValidationError as error:
                self._error(400, "VALIDATION", str(error))
            except NotFoundError:
                self._error(404, "NOT_FOUND", "자료를 찾을 수 없습니다.")
            except ConflictError as error:
                self._error(409, "CONFLICT", str(error))
            except (TimeoutError, ConnectionError):
                self.close_connection = True
            except Exception:
                self._error(500, "INTERNAL_ERROR", "요청 처리에 실패했습니다. 원본은 변경되지 않았습니다.")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_OPTIONS(self):
            self._error(405, "METHOD_DENIED", "다른 출처 연결은 v0.1에서 지원하지 않습니다.")

    return ThreadingHTTPServer((host, port), Handler)
