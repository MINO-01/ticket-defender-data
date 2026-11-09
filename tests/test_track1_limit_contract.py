"""Backend track1-purchase-limit-analysis-v2.yaml 예시와 FastAPI 구현의 일치를 검증합니다."""

import copy
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core.dependencies import get_graph_service
from main import app
from services.graph_service import GraphAnalysisUnavailable, GraphService
from tests.test_track1_analysis import FakeDriver, FakeSession

KEY = "t" * 40
PATH = "/api/v2/clusters/analyze"
INTERNAL_A, PERFORMANCE_A = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
INTERNAL_B, PERFORMANCE_B = "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
PAY = "a" * 64
DEV = "b" * 64


def policy(maximum=2, version="event-a-r1", until=None):
    return {"maxTicketsPerAccount": maximum, "policyVersion": version,
            "effectiveFrom": "2026-11-01T00:00:00Z", "effectiveUntil": until}


def scope(internal=INTERNAL_A, event="event-a", performance=PERFORMANCE_A, policy_value=None, **overrides):
    data = {
        "internalEventId": internal, "eventId": event, "performanceId": performance,
        "policyVerificationStatus": "CONFIRMED", "policy": policy() if policy_value is None else policy_value,
        "quantityDataStatus": "COMPLETE", "quantitySourceVersion": "snapshot-a",
        "quantityAsOf": "2026-11-08T10:00:00Z", "cancellationHandling": "NETTED", "duplicateHandling": "DEDUPLICATED",
    }
    data.update(overrides)
    return data


def ticket(account="account-1", reservation="reservation-1", quantity=2, event="event-a",
           performance=PERFORMANCE_A, purchased="2026-11-02T10:00:00Z", **tokens):
    return {"accountId": account, "reservationNo": reservation, "eventId": event, "performanceId": performance,
            "purchasedAt": purchased, "effectiveTicketQuantity": quantity, **tokens}


def body(tickets, scopes=None):
    return {"requestId": "2136558c-2326-4430-a46d-0793494e9b49",
            "performanceScopes": scopes or [scope()], "tickets": tickets}


class LimitContractTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TRACK1_INTERNAL_SHARED_SECRET": KEY})
        self.env.start()
        self.session = FakeSession()
        service = GraphService.__new__(GraphService)
        service.driver = FakeDriver(self.session)
        self.service = service
        app.dependency_overrides[get_graph_service] = lambda: service
        self.client = TestClient(app, headers={"X-TD-Track1-Key": KEY})

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.env.stop()

    def post(self, payload):
        return self.client.post(PATH, json=payload)

    def evaluation(self, payload, index=0):
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["purchaseLimitEvaluations"][index]

    def test_authentication_is_required_before_body(self):
        for path in (PATH, PATH + "/"):
            with self.subTest(path=path):
                response = TestClient(app).post(path, content="{")
                self.assertEqual(response.status_code, 401)

    def test_within_limit_example_shape(self):
        response = self.post(body([ticket(paymentHash=PAY, deviceIdHash=DEV)]))
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(set(data), {
            "requestId", "status", "algorithm", "algorithmVersion", "analyzedAt", "clusters",
            "purchaseLimitEvaluations",
        })
        self.assertEqual(data["clusters"], [])
        self.assertEqual(data["purchaseLimitEvaluations"], [{
            "internalEventId": INTERNAL_A, "eventId": "event-a", "performanceId": PERFORMANCE_A,
            "status": "EVALUATED", "policyVersion": "event-a-r1", "accountLimitFindings": [],
        }])

    def test_three_tickets_are_finding_without_cluster(self):
        response = self.post(body([ticket(quantity=3, paymentHash=PAY)]))
        data = response.json()
        self.assertEqual(data["clusters"], [])
        self.assertEqual(data["purchaseLimitEvaluations"][0]["accountLimitFindings"], [{
            "accountId": "account-1", "effectiveTicketQuantity": 3, "maxTicketsPerAccount": 2,
            "policyVersion": "event-a-r1",
            "reservationQuantities": [{"reservationNo": "reservation-1", "effectiveTicketQuantity": 3}],
        }])

    def test_quantities_sum_across_reservations_of_one_account(self):
        result = self.evaluation(body([
            ticket(reservation="r1", quantity=2, paymentHash=PAY), ticket(reservation="r2", quantity=1, paymentHash=PAY),
        ]))
        finding = result["accountLimitFindings"][0]
        self.assertEqual(finding["effectiveTicketQuantity"], 3)
        self.assertEqual([r["reservationNo"] for r in finding["reservationQuantities"]], ["r1", "r2"])

    def test_two_accounts_with_two_each_have_no_finding(self):
        self.session.rows = [
            {"accountIndex": 0, "communityId": 1}, {"accountIndex": 1, "communityId": 1},
            {"accountIndex": -1, "communityId": 1},
        ]
        data = self.post(body([
            ticket("a", "r1", paymentHash=PAY), ticket("b", "r2", paymentHash=PAY),
        ])).json()
        self.assertEqual(data["purchaseLimitEvaluations"][0]["status"], "EVALUATED")
        self.assertEqual(data["purchaseLimitEvaluations"][0]["accountLimitFindings"], [])
        self.assertEqual(len(data["clusters"]), 1)

    def test_mixed_performances_keep_separate_policies_and_graph_scope(self):
        scopes = [
            scope(policy_value=policy(2, "a-r1")),
            scope(INTERNAL_B, "event-b", PERFORMANCE_B, policy(4, "b-r4")),
        ]
        tickets = [
            ticket(reservation="ra", quantity=3, paymentHash=PAY),
            ticket(reservation="rb", quantity=3, event="event-b", performance=PERFORMANCE_B, paymentHash=PAY),
        ]
        data = self.post(body(tickets, scopes)).json()
        by_version = {e["policyVersion"]: e for e in data["purchaseLimitEvaluations"]}
        self.assertEqual(len(by_version["a-r1"]["accountLimitFindings"]), 1)
        self.assertEqual(by_version["b-r4"]["accountLimitFindings"], [])
        self.assertEqual(data["clusters"], [])

    def test_same_event_different_performance_do_not_connect(self):
        scopes = [scope(), scope(performance=PERFORMANCE_B)]
        tickets = [ticket("a", "r1", paymentHash=PAY), ticket("b", "r2", performance=PERFORMANCE_B, paymentHash=PAY)]
        self.assertEqual(self.post(body(tickets, scopes)).json()["clusters"], [])
        self.assertFalse(any("gds.graph.project(" in q for q in self.session.queries))

    def test_not_confirmed_outcomes_never_use_default_limit(self):
        cases = {
            "POLICY_NOT_CONFIRMED": scope(policyVerificationStatus="POLICY_NOT_CONFIRMED", policy=None),
            "QUANTITY_DATA_INCOMPLETE": scope(quantityDataStatus="INCOMPLETE"),
        }
        for status, value in cases.items():
            with self.subTest(status=status):
                payload = body([ticket(quantity=9, paymentHash=PAY)], [copy.deepcopy(value)])
                if status == "POLICY_NOT_CONFIRMED":
                    payload["performanceScopes"][0]["policy"] = None
                result = self.evaluation(payload)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["accountLimitFindings"], [])
        self.assertIsNone(self.evaluation(body(
            [ticket(paymentHash=PAY)], [{**scope(policyVerificationStatus="POLICY_NOT_CONFIRMED"), "policy": None}]
        ))["policyVersion"])

    def test_every_incomplete_marker_blocks_evaluation(self):
        for field, value in (
            ("quantityDataStatus", "UNKNOWN"), ("cancellationHandling", "NOT_CONFIRMED"),
            ("duplicateHandling", "NOT_CONFIRMED"),
        ):
            with self.subTest(field=field):
                result = self.evaluation(body([ticket(quantity=9, paymentHash=PAY)], [scope(**{field: value})]))
                self.assertEqual(result["status"], "QUANTITY_DATA_INCOMPLETE")

    def test_unknown_quantity_source_is_never_evaluated(self):
        for field, value in (
            ("quantitySourceVersion", None), ("quantitySourceVersion", "   "), ("quantityAsOf", None),
        ):
            with self.subTest(field=field, value=value):
                result = self.evaluation(body([ticket(quantity=9, paymentHash=PAY)], [scope(**{field: value})]))
                self.assertEqual(result["status"], "QUANTITY_DATA_INCOMPLETE")
                self.assertEqual(result["accountLimitFindings"], [])

    def test_omitted_quantity_source_fields_are_never_evaluated(self):
        value = scope()
        del value["quantitySourceVersion"], value["quantityAsOf"]
        result = self.evaluation(body([ticket(quantity=9, paymentHash=PAY)], [value]))
        self.assertEqual(result["status"], "QUANTITY_DATA_INCOMPLETE")

    def test_purchase_after_quantity_snapshot_blocks_whole_scope(self):
        """기준 시각과 같은 구매는 평가하고, 이후 구매가 하나라도 있으면 회차 전체를 판정하지 않습니다."""

        for purchased, status in (
            ("2026-11-08T10:00:00Z", "EVALUATED"),
            ("2026-11-08T19:00:00+09:00", "EVALUATED"),
            ("2026-11-08T10:00:01Z", "QUANTITY_DATA_INCOMPLETE"),
            ("2026-11-08T19:00:01+09:00", "QUANTITY_DATA_INCOMPLETE"),
        ):
            with self.subTest(purchased=purchased):
                result = self.evaluation(body([
                    ticket(reservation="r1", quantity=3, paymentHash=PAY),
                    ticket(reservation="r2", quantity=1, purchased=purchased, paymentHash=PAY),
                ]))
                self.assertEqual(result["status"], status)
                self.assertEqual(len(result["accountLimitFindings"]), 1 if status == "EVALUATED" else 0)

    def test_policy_interval_boundaries(self):
        """시작 시각은 평가하고 종료 시각은 판정하지 않는 잠정 경계입니다."""

        limited = policy(until="2026-11-05T00:00:00Z")
        for purchased, status in (
            ("2026-11-01T00:00:00Z", "EVALUATED"),
            ("2026-11-04T23:59:59Z", "EVALUATED"),
            ("2026-11-05T08:59:59+09:00", "EVALUATED"),
            ("2026-11-05T09:00:00+09:00", "POLICY_TIME_NOT_APPLICABLE"),
        ):
            with self.subTest(purchased=purchased):
                payload = body([ticket(quantity=3, purchased=purchased, paymentHash=PAY)], [scope(policy_value=limited)])
                self.assertEqual(self.evaluation(payload)["status"], status)

    def test_one_outside_interval_blocks_whole_scope(self):
        payload = body([
            ticket("a", "r1", quantity=9, paymentHash=PAY),
            ticket("b", "r2", quantity=9, purchased="2026-10-01T00:00:00Z", deviceIdHash=DEV),
        ])
        self.assertEqual(self.evaluation(payload)["status"], "POLICY_TIME_NOT_APPLICABLE")

    def test_finding_has_exact_contract_fields_and_v2_error_shape(self):
        finding = self.evaluation(body([ticket(quantity=3, paymentHash=PAY)]))["accountLimitFindings"][0]
        self.assertEqual(set(finding), {
            "accountId", "effectiveTicketQuantity", "maxTicketsPerAccount", "policyVersion", "reservationQuantities",
        })
        error = self.post(body([ticket(event="other", paymentHash=PAY)])).json()
        self.assertEqual(set(error), {"code", "detail"})

    def test_purchase_outside_policy_interval_is_not_applicable(self):
        for purchased in ("2026-10-31T23:59:59Z", "2026-11-05T00:00:00Z"):
            with self.subTest(purchased=purchased):
                payload = body(
                    [ticket(quantity=9, purchased=purchased, paymentHash=PAY)],
                    [scope(policy_value=policy(until="2026-11-05T00:00:00Z"))],
                )
                result = self.evaluation(payload)
                self.assertEqual(result["status"], "POLICY_TIME_NOT_APPLICABLE")
                self.assertEqual(result["accountLimitFindings"], [])

    def test_contract_errors_have_codes_and_do_not_echo_input(self):
        duplicate = body([ticket(paymentHash=PAY)], [scope(), scope()])
        mismatch = body([ticket(event="other", paymentHash=PAY)])
        no_scope = body([ticket(performance=PERFORMANCE_B, paymentHash=PAY)])
        for bad_max in (0, -1, True, "2"):
            payload = body([ticket(paymentHash=PAY)], [scope(policy_value=policy(bad_max))])
            with self.subTest(max=bad_max):
                self.assertEqual(self.post(payload).json()["code"], "INVALID_POLICY")
        confirmed_without_policy = body([ticket(paymentHash=PAY)])
        confirmed_without_policy["performanceScopes"][0]["policy"] = None
        unconfirmed_with_policy = body([ticket(paymentHash=PAY)], [scope(policyVerificationStatus="POLICY_NOT_CONFIRMED")])
        bad_time = body([ticket(paymentHash=PAY)], [scope(policy_value=policy(until="2026-10-01T00:00:00Z"))])
        for payload, code in (
            (duplicate, "DUPLICATE_PERFORMANCE_SCOPE"), (mismatch, "TICKET_SCOPE_MISMATCH"),
            (no_scope, "TICKET_SCOPE_MISMATCH"), (confirmed_without_policy, "INVALID_POLICY"),
            (unconfirmed_with_policy, "INVALID_POLICY"), (bad_time, "POLICY_TIME_INVALID"),
        ):
            with self.subTest(code=code):
                response = self.post(payload)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn(PAY, response.text)

    def test_invalid_ticket_quantity_and_extra_fields_are_rejected(self):
        for payload in (
            body([ticket(quantity=0, paymentHash=PAY)]), body([ticket(quantity=True, paymentHash=PAY)]),
            body([ticket(paymentHash=PAY, extra="x")]), body([ticket(purchased="2026-11-02T10:00:00", paymentHash=PAY)]),
        ):
            self.assertEqual(self.post(payload).status_code, 422)

    def test_ticket_without_tokens_is_still_quantity_evaluated(self):
        result = self.evaluation(body([ticket(quantity=3)]))
        self.assertEqual(len(result["accountLimitFindings"]), 1)

    def test_gds_unavailable_is_503_not_empty_result(self):
        async def fail(_request):
            raise GraphAnalysisUnavailable()

        self.service.analyze_louvain = fail
        response = self.post(body([ticket(paymentHash=PAY)]))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("COMPLETED", response.text)

    def test_v1_response_shape_is_unchanged(self):
        response = self.client.post("/api/v1/clusters/analyze", json={
            "requestId": "2136558c-2326-4430-a46d-0793494e9b49",
            "tickets": [{"accountId": "a", "reservationNo": "r", "eventId": "e", "paymentHash": PAY}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("purchaseLimitEvaluations", response.json())
        self.assertIn("score-v2", response.json()["algorithmVersion"])


if __name__ == "__main__":
    unittest.main()
