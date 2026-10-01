"""Local Hiworks draft preparation. This module never logs in or submits a document."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from contextlib import closing
import hashlib
from html import escape
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from .models import ValidationError
from .reporting import KST, validate_report

def _text(value, label, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(label + "을 확인하세요.")
    return value.strip()

def _money(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
        raise ValidationError("금액은 0 이상의 원 단위 정수로 입력하세요.")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValidationError("금액 형식을 확인하세요.") from None
    return amount

def _evidence(ids, allowed):
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or x not in allowed for x in ids):
        raise ValidationError("저장된 근거를 하나 이상 연결하세요.")
    if len(ids) != len(set(ids)):
        raise ValidationError("중복된 근거 ID를 제거하세요.")
    return ids

def _p(value):
    return "<p>" + escape(value).replace("\n", "<br>") + "</p>"

def _table(headers, rows):
    return ("<table><thead><tr>" + "".join("<th>" + escape(x) + "</th>" for x in headers) +
            "</tr></thead><tbody>" + "".join("<tr>" + "".join("<td>" + escape(str(x)) + "</td>" for x in row) +
            "</tr>" for row in rows) + "</tbody></table>")

def _draft(kind, title, text, html, evidence_ids, metadata, warnings):
    draft = {"kind": kind, "title": title, "bodyText": text, "bodyHtml": html,
             "evidenceIds": sorted(set(evidence_ids)), "metadata": metadata,
             "warnings": warnings, "externalSubmission": False, "schemaVersion": 1}
    draft["id"] = hashlib.sha256(json.dumps(draft, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return draft

def prepare_approval(value, allowed_evidence):
    required = {"title", "fund", "asset", "instructionDate", "description", "withdrawalAccount",
                "depositAccount", "stamp", "recipients", "lines", "declaredTotal", "attachments",
                "evidenceIds", "approvalLine", "retentionYears", "securityLevel", "documentBox"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValidationError("기안문 입력 항목이 올바르지 않습니다.")
    strings = {k: _text(value[k], k, 300 if k != "description" else 8000)
               for k in ("title", "fund", "asset", "instructionDate", "description", "withdrawalAccount",
                         "depositAccount", "stamp", "documentBox")}
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", strings["instructionDate"]):
        raise ValidationError("운용지시일은 YYYY-MM-DD로 입력하세요.")
    try:
        date.fromisoformat(strings["instructionDate"])
    except ValueError:
        raise ValidationError("존재하는 운용지시일을 입력하세요.") from None
    if type(value["retentionYears"]) is not int or value["retentionYears"] not in (0, 1, 3, 5, 10):
        raise ValidationError("보존 연한을 확인하세요.")
    if not isinstance(value["securityLevel"], str) or value["securityLevel"] not in ("S", "A", "B", "C"):
        raise ValidationError("보안 등급을 확인하세요.")
    evidence_ids = _evidence(value["evidenceIds"], allowed_evidence)
    if not isinstance(value["recipients"], list) or not 1 <= len(value["recipients"]) <= 20:
        raise ValidationError("수신처를 입력하세요.")
    recipients = [_text(x, "수신처", 300) for x in value["recipients"]]
    if not isinstance(value["lines"], list) or not 1 <= len(value["lines"]) <= 100:
        raise ValidationError("청구 항목을 입력하세요.")
    rows, total = [], Decimal(0)
    for index, line in enumerate(value["lines"], 1):
        if not isinstance(line, dict) or set(line) != {"label", "supply", "vat", "evidenceIds"}:
            raise ValidationError("청구 항목 형식이 올바르지 않습니다.")
        ids = _evidence(line["evidenceIds"], set(evidence_ids))
        supply, vat = _money(line["supply"]), _money(line["vat"])
        total += supply + vat
        rows.append([index, _text(line["label"], "청구 항목", 300), f"{supply:,.0f}", f"{vat:,.0f}", f"{supply + vat:,.0f}"])
    if total <= 0 or total != _money(value["declaredTotal"]):
        raise ValidationError("청구 항목의 합계와 총 출금액이 일치해야 합니다.")
    if not isinstance(value["attachments"], list) or len(value["attachments"]) > 30:
        raise ValidationError("첨부 목록을 확인하세요.")
    attachments = []
    for attachment in value["attachments"]:
        if not isinstance(attachment, dict) or set(attachment) != {"name", "evidenceIds"}:
            raise ValidationError("첨부 형식을 확인하세요.")
        name = _text(attachment["name"], "첨부 이름", 200)
        if any(c in name for c in ("/", "\\", "\r", "\n")) or name in (".", ".."):
            raise ValidationError("첨부 이름에는 경로를 넣을 수 없습니다.")
        _evidence(attachment["evidenceIds"], set(evidence_ids))
        attachments.append(name)
    if len(set(attachments)) != len(attachments):
        raise ValidationError("중복된 첨부 이름을 제거하세요.")
    if not isinstance(value["approvalLine"], list) or len(value["approvalLine"]) > 30:
        raise ValidationError("결재선을 확인하세요.")
    for person in value["approvalLine"]:
        if (not isinstance(person, dict) or set(person) != {"role", "name"} or
                person["role"] not in ("approval", "agreement", "finance_agreement", "reference")):
            raise ValidationError("결재선 역할을 확인하세요.")
        _text(person["name"], "결재자", 100)
    overview = "\n".join(["1. 운용지시 개요", "가. 펀드명: " + strings["fund"], "나. 대상자산: " + strings["asset"],
        "다. 운용지시일: " + strings["instructionDate"], "라. 지시내용: " + strings["description"],
        "마. 출금계좌: " + strings["withdrawalAccount"], "바. 입금계좌: " + strings["depositAccount"],
        f"사. 총 출금액: 금 {total:,.0f}원", "아. 청구내역:"])
    stamp = "\n".join(["2. 날인 및 송부", "가. 날인인감: " + strings["stamp"], "나. 수신처: " + ", ".join(recipients)])
    attachment_text = "3. 첨부\n" + ("\n".join(f"{i}. {name} 1부." for i, name in enumerate(attachments, 1)) if attachments else "첨부 확인 필요") + "\n끝."
    opening = strings["fund"] + " 관련 업무를 아래와 같이 진행하고자 하오니 재가하여 주시기 바랍니다."
    body = "\n\n".join([opening, "- 아 래 -", overview,
                          "\n".join(" | ".join(map(str, row)) for row in rows), stamp, attachment_text])
    html = _p(opening) + _p("- 아 래 -") + _p(overview) + _table(["NO.", "항목", "공급가액", "부가세", "합계"], rows) + _p(stamp) + _p(attachment_text)
    warnings = ["첨부 목록은 예정 목록입니다. 실제 파일과 결재선은 하이웍스에서 확인하세요."]
    if not value["approvalLine"]:
        warnings.append("결재선 확인 필요")
    if not attachments:
        warnings.append("첨부 확인 필요")
    metadata = {k: value[k] for k in ("retentionYears", "securityLevel", "documentBox", "approvalLine")}
    metadata["attachmentNames"] = attachments
    return _draft("approval", strings["title"], body, html, evidence_ids, metadata, warnings)

def prepare_weekly(report, sources, start, end, warnings=()):
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValidationError("보고 기간과 시간대를 확인하세요.")
    ids = [source["evidence_id"] for source in sources]
    if len(ids) != len(set(ids)):
        raise ValidationError("중복된 근거 ID를 제거하세요.")
    validate_report(report, set(ids))
    notices = list(warnings)
    for source in sources:
        stamp = source.get("timestamp")
        if not stamp:
            notices.append("시각 미확인 자료 포함: 담당자 확인 필요")
            continue
        try:
            instant = datetime.fromisoformat(stamp)
        except ValueError:
            raise ValidationError("근거 시각을 확인하세요.") from None
        if instant.tzinfo is None or not start <= instant < end:
            raise ValidationError("보고 기간 밖의 근거가 포함되어 있습니다.")
    label = f"{start.astimezone(KST):%Y-%m-%d %H:%M} ~ {end.astimezone(KST):%Y-%m-%d %H:%M}"
    title = f"주간업무보고 ({end.astimezone(KST):%Y-%m-%d} 기준)"
    rows = [[item["project"], item["work"], item["result"], item["issues"], item["next_week"],
             ", ".join(item["evidence_ids"])] for item in report["items"]]
    period = "보고 기간: " + label + " (한국시간, 종료시각 미포함)"
    notice = "확인 사항: " + ("; ".join(dict.fromkeys(notices)) or "담당자 검토 후 사용")
    text = "\n\n".join([period, "\n".join(" | ".join(row) for row in rows), notice])
    html = _p(period) + _table(["프로젝트", "업무", "진행·결과", "이슈", "차주 계획", "근거"], rows) + _p(notice)
    return _draft("weekly", title, text, html, [x for item in report["items"] for x in item["evidence_ids"]],
                  {"periodStart": start.isoformat(), "periodEnd": end.isoformat(), "formConfirmed": False},
                  list(dict.fromkeys(notices)) + ["회사 주간보고 양식과 제출 경로 확인 필요"])

def build_page_link_payload(draft, form_id, callback_url, state):
    """Build a documented API body only; no request, tokens or automatic submission."""
    form_id = _text(form_id, "API 양식 ID", 200)
    state = _text(state, "요청 상관 ID", 200)
    callback = urlsplit(callback_url)
    if callback.scheme != "https" or not callback.hostname or callback.username or callback.password or callback.fragment:
        raise ValidationError("승인된 HTTPS 콜백 주소를 사용하세요.")
    if draft["metadata"].get("attachmentNames"):
        raise ValidationError("파일 첨부 연동 검증 후 API 패키지를 생성할 수 있습니다.")
    return {"form_id": form_id, "subject": draft["title"], "contents": draft["bodyHtml"],
            "callback_url": callback_url, "modify_contents_flag": "Y", "modify_files_flag": "Y", "state": state}

class DraftStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS hiworks_drafts "
                               "(id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, "
                               "payload TEXT NOT NULL, created_at TEXT NOT NULL)")
    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)
    def save(self, draft):
        payload = json.dumps(draft, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO hiworks_drafts VALUES(?,?,?,?,?)",
                (draft["id"], draft["kind"], draft["title"], payload, datetime.now(timezone.utc).isoformat()))
        return draft
    def list(self):
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT id,kind,title,created_at FROM hiworks_drafts ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(zip(("id", "kind", "title", "createdAt"), row)) for row in rows]
    def get(self, identifier):
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT payload FROM hiworks_drafts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            from .repository import NotFoundError
            raise NotFoundError("Draft not found")
        return json.loads(row[0])
