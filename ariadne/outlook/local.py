"""Manual classic-Outlook collection; isolated STA process, no Graph credentials.

Only server-issued folder keys cross the HTTP boundary. Background threads do
not touch COM: a short-lived Windows PowerShell STA process owns all COM calls.
Jobs are session-local; archived messages survive restarts and are idempotent.
"""

from base64 import b64encode
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock, Thread
from time import monotonic
from uuid import uuid4

from ..mail.models import validate_capture
from ..models import ValidationError
from .errors import OutlookError


KST = timezone(timedelta(hours=9))
MAX_OUTPUT = 24 * 1024 * 1024


class PowerShellOutlook:
    """No shell interpolation, execution-policy changes, credentials or downloads."""

    def __init__(self):
        self.executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"

    @property
    def supported(self):
        return sys.platform == "win32" and self.executable.is_file()

    def run(self, payload):
        if not self.supported:
            raise OutlookError("LOCAL_OUTLOOK_UNAVAILABLE", "Windows PC의 클래식 Outlook에서 사용할 수 있습니다.", 503)
        script = Path(__file__).with_name("local_reader.ps1").read_text(encoding="utf-8")
        command = [str(self.executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Sta",
                   "-EncodedCommand", b64encode(script.encode("utf-16-le")).decode("ascii")]
        # Command text is fixed package code. Folder IDs and date inputs travel
        # only over JSON stdin. Do not log either stream or provider exceptions.
        try:
            result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=True).encode("ascii"),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise OutlookError("LOCAL_OUTLOOK_TIMEOUT", "Outlook 응답 시간이 초과되었습니다. 접근 확인 창과 Outlook 상태를 확인해 주세요.", 504) from None
        except OSError:
            raise OutlookError("LOCAL_OUTLOOK_START", "Outlook 읽기 프로세스를 시작하지 못했습니다.", 503) from None
        if result.returncode or len(result.stdout) > MAX_OUTPUT:
            raise OutlookError("LOCAL_OUTLOOK_READ", "Outlook 읽기에 실패했습니다. Outlook 로그인과 프로그램 접근 정책을 확인해 주세요.", 503)
        try:
            value = json.loads(result.stdout.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise OutlookError("LOCAL_OUTLOOK_RESPONSE", "Outlook 읽기 결과를 확인하지 못했습니다.", 503) from None
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise OutlookError("LOCAL_OUTLOOK_READ", "Outlook 읽기에 실패했습니다. 클래식 Outlook을 열고 접근 확인 창이 있는지 확인해 주세요.", 503)
        return value


def _bounded(value, maximum=2048):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValidationError("Invalid local Outlook identifier")
    return value


class LocalOutlookService:
    def __init__(self, repository, adapter=None, clock=monotonic):
        self.repository = repository
        self.adapter = adapter or PowerShellOutlook()
        self.clock = clock
        self.lock = RLock()
        self.jobs = {}
        self.folders = {}
        self.catalog_expires = 0
        self.active = None
        self.closed = False
        self.worker = None

    def status(self):
        with self.lock:
            return {"supported": self.adapter.supported, "activeJobId": self.active,
                    "lastJobId": next(reversed(self.jobs), None), "maxMessages": 50,
                    "message": "회사 PC의 클래식 Outlook을 실행한 상태에서 사용하세요. 앱 키는 필요하지 않습니다." if self.adapter.supported else "Windows PC의 클래식 Outlook이 필요합니다."}

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise OutlookError("LOCAL_JOB_NOT_FOUND", "수집 기록이 없거나 서버가 재시작되었습니다. 저장된 메일은 근거함에서 확인해 주세요.", 404)
            return self._public(self.jobs[job_id])

    def start(self, kind, request_id, **query):
        _bounded(request_id, 100)
        if kind not in {"discover", "collect"}:
            raise ValidationError("Invalid local operation")
        fingerprint = json.dumps([kind, query], sort_keys=True, ensure_ascii=True)
        with self.lock:
            # HTTP retry is the same operation, including after a lost response.
            for job in self.jobs.values():
                if job["requestId"] == request_id:
                    if job["fingerprint"] != fingerprint:
                        raise OutlookError("LOCAL_REQUEST_CONFLICT", "같은 요청의 수집 조건이 바뀌었습니다.", 409)
                    return self._public(job)
            if self.closed or self.active:
                raise OutlookError("LOCAL_OUTLOOK_BUSY", "진행 중인 Outlook 작업이 끝난 뒤 다시 실행해 주세요.", 409)
            if not self.adapter.supported:
                raise OutlookError("LOCAL_OUTLOOK_UNAVAILABLE", "Windows PC의 클래식 Outlook이 필요합니다.", 503)
            selected = None
            if kind == "collect":
                selected = self._validate_query(query)
            job_id = str(uuid4())
            job = {"id": job_id, "kind": kind, "requestId": request_id, "fingerprint": fingerprint,
                   "state": "running", "stage": "reading", "created": 0, "unchanged": 0,
                   "revised": 0, "failed": 0, "processed": 0, "total": 0, "message": "Outlook에서 읽는 중입니다.",
                   "truncated": False, "folders": [], "warnings": 0}
            while len(self.jobs) >= 20:
                del self.jobs[next(iter(self.jobs))]
            self.jobs[job_id] = job
            self.active = job_id
            self.worker = Thread(target=self._run, args=(job_id, kind, query, selected), daemon=True)
            self.worker.start()
            return self._public(job)

    @staticmethod
    def _public(job):
        return {key: deepcopy(value) for key, value in job.items() if key != "fingerprint"}

    def _validate_query(self, query):
        if set(query) != {"folderKey", "basis", "startDate", "endDate", "limit", "actorId"}:
            raise ValidationError("Invalid collection fields")
        _bounded(query["folderKey"], 100)
        if self.clock() >= self.catalog_expires or query["folderKey"] not in self.folders:
            raise OutlookError("LOCAL_FOLDERS_EXPIRED", "폴더 목록을 다시 불러와 선택해 주세요.", 409)
        if query["basis"] not in {"inbox", "sentitems"}:
            raise ValidationError("Select received or sent time")
        if type(query["limit"]) is not int or not 1 <= query["limit"] <= 50:
            raise ValidationError("Collection limit is 1..50")
        actor = _bounded(query["actorId"], 200)
        if actor.strip().casefold() in {"ai", "system"}:
            raise ValidationError("Collection requires a user actor")
        try:
            start, end = date.fromisoformat(query["startDate"]), date.fromisoformat(query["endDate"])
            if start.isoformat() != query["startDate"] or end.isoformat() != query["endDate"] or not 0 <= (end - start).days < 31:
                raise ValueError()
            end + timedelta(days=1)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("Select a valid KST date range of at most 31 days") from None
        return deepcopy(self.folders[query["folderKey"]])

    def _update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)

    def _run(self, job_id, kind, query, selected):
        try:
            if kind == "discover":
                self._discover(job_id)
            else:
                self._collect(job_id, query, selected)
        except OutlookError as error:
            self._update(job_id, state="failed", stage="finished", message=error.message)
        except Exception:
            self._update(job_id, state="failed", stage="finished", message="작업을 완료하지 못했습니다. 저장 건수를 확인한 뒤 같은 범위로 다시 수집할 수 있습니다.")
        finally:
            with self.lock:
                self.active = None

    def _discover(self, job_id):
        result = self.adapter.run({"operation": "discover"})
        entries = result.get("folders")
        if not isinstance(entries, list) or len(entries) > 2000:
            raise ValidationError("Invalid folder catalog")
        folders, public = {}, []
        for entry in entries:
            entry = {name: _bounded(entry[name], 2048 if name.endswith("Id") else 1000)
                     for name in ("storeId", "folderId", "storeName", "folderPath")}
            key = str(uuid4())
            folders[key] = entry
            public.append({"key": key, "accountKey": sha256(entry["storeId"].encode()).hexdigest(),
                           "account": entry["storeName"], "path": entry["folderPath"]})
        with self.lock:
            self.folders = folders
            self.catalog_expires = self.clock() + 1800
        warnings = result.get("warnings", 0)
        self._update(job_id, state="completed", stage="finished", folders=public, warnings=warnings,
                     truncated=bool(result.get("truncated")), message=f"메일 폴더 {len(public)}개를 확인했습니다. 선택한 폴더만 수집하며 하위 폴더는 각각 선택합니다.")

    def _collect(self, job_id, query, selected):
        result = self.adapter.run({"operation": "collect", **selected,
                                   **{key: query[key] for key in ("basis", "startDate", "endDate", "limit")}})
        messages = result.get("messages")
        failed = result.get("failed", 0)
        if not isinstance(messages, list) or len(messages) > query["limit"] or type(failed) is not int or not 0 <= failed <= query["limit"] - len(messages):
            raise ValidationError("Invalid local collection result")
        namespace = sha256(("outlook-com-v1\0" + selected["storeId"]).encode()).hexdigest()
        folder_hash = sha256(selected["folderId"].encode()).hexdigest()
        captured_at = datetime.now(timezone.utc).isoformat()
        counts = {"created": 0, "unchanged": 0, "revised": 0, "failed": failed}
        self._update(job_id, stage="saving", total=len(messages) + failed, failed=failed,
                     processed=failed, truncated=bool(result.get("truncated")), message="읽은 메일을 근거함에 저장하고 있습니다.")
        for index, message in enumerate(messages):
            with self.lock:
                if self.closed:
                    raise OutlookError("LOCAL_STOPPED", "서버 종료로 수집이 중단되었습니다. 같은 범위로 다시 수집해 주세요.", 409)
                try:
                    # IDs from COM are namespaced separately from Graph. Never
                    # merge different stores/copies by Internet Message-ID.
                    message = deepcopy(message)
                    message["id"] = "com:" + sha256(_bounded(message["id"]).encode()).hexdigest()
                    message["localSource"] = {"kind": "outlook_com", "folderId": folder_hash,
                                              "folderPath": selected["folderPath"], "timeBasis": query["basis"]}
                    capture = {"mailbox": {"id": namespace, "username": selected["storeName"][:500]},
                               "folder": query["basis"], "fetchedAt": captured_at, "messages": [message]}
                    validate_capture(capture)
                    saved = self.repository.mail.import_messages(capture, query["actorId"].strip())
                    for key in ("created", "unchanged", "revised"):
                        counts[key] += saved[key]
                except Exception:
                    counts["failed"] += 1
                self.jobs[job_id].update(**counts, processed=failed + index + 1)
        state = "partial" if counts["failed"] else "completed"
        self._update(job_id, state=state, stage="finished", **counts,
                     message="수집을 마쳤습니다. 실패 건은 같은 범위로 다시 수집하면 됩니다." if counts["failed"] else "수집을 마쳤습니다. 메일 근거함에서 확인하세요.")

    def close(self):
        # Joining is unnecessary: after this lock returns no worker can enter
        # another repository write. The bounded child read can finish on its own.
        with self.lock:
            self.closed = True
