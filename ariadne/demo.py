"""A reproducible synthetic dataset; contains no real company records."""

from .repository import NotFoundError


def seed_demo(repository) -> dict:
    if not any(fund["id"] == "synthetic-fund-a" for fund in repository.list_funds()):
        repository.register_fund("synthetic-fund-a", "가상 펀드 A", ["Synthetic Fund A", "가상 SPC A"], actor_id="demo-user")
    timestamp = "2026-10-01T00:00:00+09:00"
    evidence = {
        "id": "synthetic-request-001",
        "provider": "synthetic",
        "sourceId": "synthetic-note-001",
        "capturedAt": timestamp,
        "evidence": "가상 펀드 A의 날인본을 요청했습니다. 최종 완료에는 날인 상태 검토가 필요합니다.",
        "locator": "synthetic://demo/request-001",
        "sha256": None,
        "threadId": "synthetic-thread-001",
        "finality": "unknown",
        "role": "email_sent",
    }
    repository.add_evidence(evidence, actor_id="demo-user")
    item = {
        "id": "synthetic-task-001",
        "title": "[가상] 펀드 A 날인본 수령·검토",
        "fundId": "synthetic-fund-a",
        "ownerId": None,
        "status": "waiting_external",
        "reviewRequired": True,
        "confidence": "medium",
        "due": {
            "kind": "unknown", "rawText": "기한 미정", "date": None,
            "timezone": "Asia/Seoul", "anchorTaskId": None,
            "confirmed": False, "evidenceIds": [],
        },
        "sources": [evidence["id"]],
        "completionConditions": [{"id": "review-stamp", "label": "날인·서명 및 첨부파일 검토 완료"}],
        "completionEvidence": [],
        "waitingReason": "가상 상대방의 날인본 회신 대기",
    }
    try:
        return repository.get_work_item(item["id"])
    except NotFoundError:
        return repository.create_work_item(item, actor_id="demo-user", reason="가상 데이터 시연")
