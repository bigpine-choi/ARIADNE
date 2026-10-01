from copy import deepcopy
import unittest

from ariadne.funds import FundRegistry
from ariadne.models import ValidationError, validate_evidence, validate_work_item


def work_item(item_id="work-1"):
    return {"id": item_id, "title": "날인본 수령 검토", "fundId": None, "ownerId": None,
            "status": "waiting_external", "reviewRequired": True, "confidence": "medium",
            "due": {"kind": "unknown", "rawText": "일정 미확정", "date": None,
                    "timezone": "Asia/Seoul", "anchorTaskId": None, "confirmed": False, "evidenceIds": []},
            "sources": [], "completionConditions": [], "completionEvidence": []}


def source(evidence_id="evidence-1", role="context"):
    return {"id": evidence_id, "provider": "synthetic", "sourceId": evidence_id,
            "capturedAt": "2026-06-19T16:00:00+09:00", "evidence": "테스트용 자료",
            "locator": "synthetic://fixture", "sha256": None, "threadId": None,
            "finality": "unknown", "role": role}


class ModelValidationTests(unittest.TestCase):
    def test_validation_detaches_nested_data(self):
        supplied = work_item()
        result = validate_work_item(supplied)
        result["due"]["rawText"] = "수정"
        self.assertEqual(supplied["due"]["rawText"], "일정 미확정")

    def test_true_calendar_dates_and_confirmed_source_requirement(self):
        item = work_item()
        item["due"].update(kind="date", date="2026-02-29", confirmed=True, evidenceIds=["evidence-1"])
        with self.assertRaises(ValidationError):
            validate_work_item(item)
        item["due"]["date"] = "2028-02-29"
        validate_work_item(item)
        item["due"]["evidenceIds"] = []
        with self.assertRaises(ValidationError):
            validate_work_item(item)

    def test_due_variants_and_boolean_integer_rejection(self):
        base = work_item()
        valid = [
            {"kind": "window", "startDate": "2026-06-19", "endDate": "2026-06-26"},
            {"kind": "recurrence", "rule": "monthly; review required"},
            {"kind": "event", "anchorTaskId": "preceding-work"},
        ]
        for patch in valid:
            with self.subTest(kind=patch["kind"]):
                item = deepcopy(base)
                item["due"].update(patch)
                validate_work_item(item)
        invalid = [
            {"kind": "window", "startDate": "2026-06-26", "endDate": "2026-06-19"},
            {"kind": "recurrence"}, {"kind": "event"}, {"preparationDays": True},
        ]
        for patch in invalid:
            with self.subTest(patch=patch):
                item = deepcopy(base)
                item["due"].update(patch)
                with self.assertRaises(ValidationError):
                    validate_work_item(item)

    def test_unknown_fields_duplicate_ids_and_nonbool_review_rejected(self):
        for patch in [{"unexpected": "ignored?"}, {"reviewRequired": 1}, {"sources": ["a", "a"]}]:
            with self.subTest(patch=patch):
                with self.assertRaises(ValidationError):
                    validate_work_item({**work_item(), **patch})

    def test_evidence_requires_aware_timestamp_and_strict_digest(self):
        valid = source()
        valid["sha256"] = "A" * 64
        self.assertEqual(validate_evidence(valid)["sha256"], "a" * 64)
        for patch in [{"capturedAt": "2026-06-19T16:00:00"}, {"capturedAt": "2026-02-30T00:00:00Z"},
                      {"capturedAt": "2026-06-19T16:00:00+01:99"},
                      {"sha256": "deadbeef"}, {"page": True}, {"page": 0}]:
            with self.subTest(patch=patch):
                with self.assertRaises(ValidationError):
                    validate_evidence({**source(), **patch})

    def test_completion_review_requires_existing_condition_and_aware_time(self):
        item = work_item()
        item["completionConditions"] = [{"id": "review", "label": "날인본 검토"}]
        item["completionEvidence"] = [{"conditionId": "review", "evidenceId": "evidence-1",
                                      "reviewedBy": "user", "reviewedAt": "2026-06-19T18:00:00+09:00"}]
        validate_work_item(item)
        item["completionEvidence"][0]["conditionId"] = "unknown"
        with self.assertRaises(ValidationError):
            validate_work_item(item)

    def test_fund_aliases_use_exact_normalized_matching_and_reject_ambiguity(self):
        registry = FundRegistry([{"id": "demo-1", "name": "Demo Fund", "aliases": ["데모 펀드"]}])
        self.assertEqual(registry.resolve("  DEMO   FUND  "), "demo-1")
        registry.register("demo-2", "Other Fund", ["데모 펀드"])
        with self.assertRaises(ValidationError):
            registry.resolve("데모 펀드")
        with self.assertRaises(ValidationError):
            registry.resolve("Demo")


if __name__ == "__main__":
    unittest.main()
