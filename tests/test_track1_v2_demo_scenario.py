"""격리 합성 시나리오로 v2 수량 평가와 Spring 형식 호환성을 검증합니다. A3 반입 원장이 아닙니다."""

import hashlib
import json
import unittest
from pathlib import Path

from schemas.dto import Track1LimitAnalysisRequest
from services.track1_scoring import purchase_limit_evaluations

FIXTURES = Path(__file__).parent / "fixtures"
RAW = (FIXTURES / "track1_v2_demo_scenario.json").read_bytes()
MANIFEST = json.loads((FIXTURES / "track1_v2_demo_scenario.manifest.json").read_text(encoding="utf-8"))
PAYLOAD = json.loads(RAW)


def parsed() -> Track1LimitAnalysisRequest:
    return Track1LimitAnalysisRequest.model_validate(PAYLOAD)


class ScenarioContractTests(unittest.TestCase):
    def test_fixture_matches_manifest_and_is_not_an_import_ledger(self):
        self.assertEqual(hashlib.sha256(RAW.replace(b"\r\n", b"\n")).hexdigest(), MANIFEST["requestSha256"])
        self.assertIs(MANIFEST["synthetic"], True)
        self.assertIs(MANIFEST["finalForImport"], False)

    def test_spring_explicit_null_fields_are_accepted(self):
        legacy = {
            "accountId": "a", "reservationNo": "r", "eventId": "demo-event-a",
            "performanceId": PAYLOAD["performanceScopes"][0]["performanceId"], "purchasedAt": "2026-11-02T10:00:00Z",
            "effectiveTicketQuantity": 3, "paymentHash": "a" * 64, "addressHash": None, "deviceIdHash": None, "ipHash": None,
            "quantityInputType": None, "ticketReference": None, "ticketStatus": None, "sourceVersion": None, "sourceAsOf": None,
        }
        request = Track1LimitAnalysisRequest.model_validate({
            "requestId": PAYLOAD["requestId"], "performanceScopes": [PAYLOAD["performanceScopes"][0]], "tickets": [legacy],
        })
        result = purchase_limit_evaluations(request)[0]
        self.assertEqual(result.status, "EVALUATED")
        self.assertEqual(result.account_limit_findings[0].effective_ticket_quantity, 3)

    def test_each_scope_keeps_its_own_outcome(self):
        results = {item.performance_id: item for item in purchase_limit_evaluations(parsed())}
        self.assertEqual({key: item.status for key, item in results.items()}, MANIFEST["expected"]["evaluations"])
        for item in results.values():
            if item.status != "EVALUATED":
                self.assertEqual(item.account_limit_findings, [])

    def test_only_the_single_account_over_limit_is_flagged(self):
        evaluated = next(item for item in purchase_limit_evaluations(parsed()) if item.status == "EVALUATED")
        findings = [(f.account_id, f.effective_ticket_quantity) for f in evaluated.account_limit_findings]
        expected = [(f["accountId"], f["effectiveTicketQuantity"]) for f in MANIFEST["expected"]["limitFindings"]]
        self.assertEqual(findings, expected)
        # 여러 줄이어도 예매 하나의 합계로 표시합니다.
        reservations = evaluated.account_limit_findings[0].reservation_quantities
        self.assertEqual([(r.reservation_no, r.effective_ticket_quantity) for r in reservations], [("over-res", 3)])


if __name__ == "__main__":
    unittest.main()
