"""Track 1 내부 인증 게이트와 레거시 /macro 차단을 합성 요청으로 검증합니다."""

import logging
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.dependencies import get_graph_service, get_graph_service_factory
from main import app
from schemas.dto import Track1AnalysisResponse
from services.graph_service import GraphAnalysisUnavailable, GraphResultInvalid


ANALYZE = "/api/v1/clusters/analyze"
MULTI_EVENT_ANALYZE = "/api/v2/multi-event/analyze"
MACRO = "/api/v1/clusters/macro"
VLM_PATH = "/api/v1/parse/ticket-image"
TRACK1_KEY = "track1-synthetic-key-" + "k" * 24
VLM_KEY = "vlm-synthetic-key-" + "v" * 24
REQUEST_ID = "2136558c-2326-4430-a46d-0793494e9b49"
PAYLOAD = {
    "requestId": REQUEST_ID,
    "tickets": [
        {"accountId": f"uid-{n}", "reservationNo": f"res-{n}", "eventId": "event-1",
         "paymentHash": "a" * 64, "addressHash": None, "deviceIdHash": None, "ipHash": None}
        for n in (1, 2)
    ],
}
LEGACY_PAYLOAD = {"tickets": [{
    "accountId": "uid-1", "paymentHash": "a" * 64, "addressHash": "a" * 64,
    "deviceIdHash": "c" * 64, "ipHash": "a" * 64,
}]}


class RecordingGraphService:
    """그래프 작업 호출 여부만 기록하는 Fake GraphService입니다."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def analyze_louvain(self, request):
        self.calls.append("analyze_louvain")
        if self.error:
            raise self.error
        return Track1AnalysisResponse(
            requestId=request.request_id, status="COMPLETED", algorithm="LOUVAIN",
            algorithmVersion="gds-2.6.6;projection-v1;score-v1",
            analyzedAt=datetime.now(timezone.utc), clusters=[],
        )

    async def load_json_to_graph(self, tickets):
        self.calls.append("load_json_to_graph")

    async def detect_macro_clusters(self, threshold=5):
        self.calls.append("detect_macro_clusters")
        return []

    async def analyze_multi_event_exp2(self, request):
        self.calls.append("analyze_multi_event_exp2")
        if self.error:
            raise self.error
        return {scope.key: [] for scope in request.ledger_scopes}, "gds-test;projection-test;score-test"


def multi_event_payload():
    first_performance, second_performance = str(uuid4()), str(uuid4())
    first_ledger, second_ledger = str(uuid4()), str(uuid4())
    return {
        "analysisId": str(uuid4()),
        "tenantId": str(uuid4()),
        "ledgerVersionSetDigest": "d" * 64,
        "analysisAsOf": "2026-01-01T00:00:00Z",
        "analysisStartAt": "2025-12-01T00:00:00Z",
        "analysisEndAt": "2026-01-01T00:00:00Z",
        "scoreVersion": "score-v2-exp2-multi-event-recurrence",
        "projectionVersion": "projection-v2.2-exp2-ipwindow-softhub",
        "ledgerScopes": [
            {"eventId": "event-a", "performanceId": first_performance, "ledgerVersionId": first_ledger, "sourceBatchId": "batch-a"},
            {"eventId": "event-b", "performanceId": second_performance, "ledgerVersionId": second_ledger, "sourceBatchId": "batch-b"},
        ],
        "tickets": [{
            "accountId": "a" * 64, "reservationNo": "b" * 64, "eventId": "event-a",
            "performanceId": first_performance, "ledgerVersionId": first_ledger,
            "supplierNamespace": "supplier-a", "hmacPurpose": "TRACK1_CONNECTION",
            "hmacKeyVersion": "key-v1", "sourceVersion": "source-v1", "refundAccountHash": "e" * 64,
        }],
    }


class Track1InternalAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TRACK1_INTERNAL_SHARED_SECRET": TRACK1_KEY,
            "VLM_INTERNAL_SHARED_SECRET": VLM_KEY,
            "VLM_ENABLED": "true",
        })
        self.env.start()
        self.service = RecordingGraphService()
        self.dependency_calls = 0
        self.factory_error = None

        def provide():
            self.dependency_calls += 1
            return self.service

        def provide_factory():
            async def acquire():
                self.dependency_calls += 1
                if self.factory_error is not None:
                    raise self.factory_error
                return self.service

            return acquire

        app.dependency_overrides[get_graph_service] = provide
        app.dependency_overrides[get_graph_service_factory] = provide_factory
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.env.stop()

    def assert_graph_untouched(self):
        self.assertEqual(self.dependency_calls, 0)
        self.assertEqual(self.service.calls, [])

    def test_valid_key_runs_existing_analysis_contract(self):
        response = self.client.post(ANALYZE, json=PAYLOAD, headers={"X-TD-Track1-Key": TRACK1_KEY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {
            "requestId", "status", "algorithm", "algorithmVersion", "analyzedAt", "clusters", "sharedSignalObservations",
        })
        self.assertEqual(response.json()["requestId"], REQUEST_ID)
        self.assertEqual(self.service.calls, ["analyze_louvain"])

    def test_missing_wrong_and_duplicate_credentials_return_401(self):
        cases = {
            "missing": [],
            "wrong": [("X-TD-Track1-Key", "x" * 40)],
            "prefix": [("X-TD-Track1-Key", TRACK1_KEY[:-1])],
            "duplicate": [("X-TD-Track1-Key", TRACK1_KEY), ("X-TD-Track1-Key", TRACK1_KEY)],
            "track2_header_only": [("X-TD-Internal-Key", TRACK1_KEY)],
            "vlm_key": [("X-TD-Track1-Key", VLM_KEY)],
            "bearer_fallback": [("Authorization", f"Bearer {TRACK1_KEY}")],
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                response = self.client.post(ANALYZE, json=PAYLOAD, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": [{"code": "INTERNAL_AUTH_REQUIRED"}]})
                self.assertNotIn(TRACK1_KEY, response.text)
        self.assert_graph_untouched()

    def test_authentication_runs_before_body_validation(self):
        response = self.client.post(
            ANALYZE, content="{", headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 401)
        response = self.client.post(ANALYZE, json={"requestId": "not-a-uuid", "tickets": []})
        self.assertEqual(response.status_code, 401)
        self.assert_graph_untouched()

    def test_root_path_and_trailing_slash_are_gated(self):
        response = self.client.post(ANALYZE + "/", json=PAYLOAD, follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        with TestClient(app, root_path="/td") as client:
            response = client.post(ANALYZE, json=PAYLOAD)
        self.assertEqual(response.status_code, 401)
        self.assert_graph_untouched()

    def test_multi_event_path_is_gated_before_body_validation(self):
        response = self.client.post(MULTI_EVENT_ANALYZE, content="{", headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 401)
        response = self.client.post(MULTI_EVENT_ANALYZE, json={}, headers={"X-TD-Track1-Key": TRACK1_KEY})
        self.assertEqual(response.status_code, 422)
        self.assert_graph_untouched()

    def test_multi_event_missing_wrong_and_duplicate_credentials_do_not_acquire_graph(self):
        cases = {
            "missing": [],
            "wrong": [("X-TD-Track1-Key", "x" * 40)],
            "duplicate": [("X-TD-Track1-Key", TRACK1_KEY), ("X-TD-Track1-Key", TRACK1_KEY)],
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                response = self.client.post(MULTI_EVENT_ANALYZE, json=multi_event_payload(), headers=headers)
                self.assertEqual(response.status_code, 401)
        self.assert_graph_untouched()

    def test_multi_event_authentication_configuration_error_does_not_acquire_graph(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRACK1_INTERNAL_SHARED_SECRET", None)
            response = self.client.post(
                MULTI_EVENT_ANALYZE, json=multi_event_payload(), headers={"X-TD-Track1-Key": TRACK1_KEY},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": [{"code": "TRACK1_AUTH_UNCONFIGURED"}]})
        self.assert_graph_untouched()

    def test_multi_event_trailing_slash_and_root_path_remain_gated(self):
        response = self.client.post(MULTI_EVENT_ANALYZE + "/", json=multi_event_payload(), follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        with TestClient(app, root_path="/td") as client:
            response = client.post(MULTI_EVENT_ANALYZE, json=multi_event_payload())
        self.assertEqual(response.status_code, 401)
        self.assert_graph_untouched()

    def test_multi_event_validated_body_acquires_service_and_runs_analysis(self):
        response = self.client.post(
            MULTI_EVENT_ANALYZE, json=multi_event_payload(), headers={"X-TD-Track1-Key": TRACK1_KEY},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["candidates"], [])
        self.assertEqual(self.dependency_calls, 1)
        self.assertEqual(self.service.calls, ["analyze_multi_event_exp2"])

    def test_multi_event_gds_and_http_service_failures_keep_503(self):
        cases = (GraphAnalysisUnavailable(), HTTPException(status_code=503, detail="Service unavailable"))
        for error in cases:
            with self.subTest(error=type(error).__name__):
                self.service = RecordingGraphService(error)
                self.factory_error = error if isinstance(error, HTTPException) else None
                if not isinstance(error, HTTPException):
                    self.service.error = error
                response = self.client.post(
                    MULTI_EVENT_ANALYZE, json=multi_event_payload(), headers={"X-TD-Track1-Key": TRACK1_KEY},
                )
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("candidates", response.text)
        self.assertEqual(self.dependency_calls, 2)

    def test_multi_event_analysis_failure_is_not_an_empty_success(self):
        self.service = RecordingGraphService(RuntimeError("synthetic analysis failure"))
        response = self.client.post(
            MULTI_EVENT_ANALYZE, json=multi_event_payload(), headers={"X-TD-Track1-Key": TRACK1_KEY},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": {"code": "MULTI_EVENT_ANALYSIS_FAILED"}})
        self.assertNotIn("candidates", response.text)
        self.assertEqual(self.dependency_calls, 1)

    def test_multi_event_validation_error_does_not_echo_input_tokens_or_secrets(self):
        sentinels = (
            "RAW_ANALYSIS_IDENTIFIER_SENTINEL", "RAW_TENANT_IDENTIFIER_SENTINEL",
            "PSEUDONYM_ACCOUNT_SENTINEL", "HMAC_TOKEN_SENTINEL", TRACK1_KEY,
        )
        response = self.client.post(
            MULTI_EVENT_ANALYZE,
            json={
                "analysisId": sentinels[0], "tenantId": sentinels[1],
                "ledgerVersionSetDigest": sentinels[3],
                "tickets": [{"accountId": sentinels[2], "refundAccountHash": sentinels[3]}],
            },
            headers={"X-TD-Track1-Key": TRACK1_KEY},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": [{"code": "INVALID_MULTI_EVENT_ANALYSIS_REQUEST"}]})
        for sentinel in sentinels:
            self.assertNotIn(sentinel, response.text)
        self.assert_graph_untouched()

    def test_missing_short_or_shared_secret_fails_closed_with_503(self):
        cases = {
            "unset": None,
            "empty": "",
            "short": "k" * 31,
            "same_as_track2": VLM_KEY,
        }
        for name, secret in cases.items():
            with self.subTest(case=name):
                with patch.dict(os.environ, {}, clear=False):
                    if secret is None:
                        os.environ.pop("TRACK1_INTERNAL_SHARED_SECRET", None)
                    else:
                        os.environ["TRACK1_INTERNAL_SHARED_SECRET"] = secret
                    sent = secret if secret else TRACK1_KEY
                    response = self.client.post(ANALYZE, json=PAYLOAD, headers={"X-TD-Track1-Key": sent})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"detail": [{"code": "TRACK1_AUTH_UNCONFIGURED"}]})
        self.assert_graph_untouched()

    def test_minimum_length_is_counted_in_utf8_bytes(self):
        multibyte = "가" * 11  # 33 UTF-8 바이트
        with patch.dict(os.environ, {"TRACK1_INTERNAL_SHARED_SECRET": multibyte}):
            response = self.client.post(
                ANALYZE, json=PAYLOAD, headers={"X-TD-Track1-Key": multibyte.encode("utf-8")},
            )
        self.assertEqual(response.status_code, 200)

    def test_legacy_macro_returns_410_without_graph_work(self):
        for headers in ({}, {"X-TD-Track1-Key": TRACK1_KEY}):
            with self.subTest(authenticated=bool(headers)):
                response = self.client.post(MACRO, json=LEGACY_PAYLOAD, headers=headers)
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response.json(), {"detail": [{"code": "LEGACY_ENDPOINT_REMOVED"}]})
                self.assertEqual(response.headers["cache-control"], "no-store")
        response = self.client.post(MACRO + "?threshold=2", content="{", headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 410)
        self.assertNotIn(MACRO, app.openapi()["paths"])
        self.assert_graph_untouched()

    def test_authenticated_analysis_failure_stays_failure(self):
        for error, status in ((GraphAnalysisUnavailable(), 503), (GraphResultInvalid(), 500)):
            with self.subTest(error=type(error).__name__):
                self.service = RecordingGraphService(error)
                response = self.client.post(ANALYZE, json=PAYLOAD, headers={"X-TD-Track1-Key": TRACK1_KEY})
                self.assertEqual(response.status_code, status)
                self.assertNotIn("COMPLETED", response.text)
                self.assertNotIn("clusters", response.text)

    def test_logs_do_not_contain_secret_or_credential(self):
        sent = "attacker-supplied-" + "z" * 30
        with self.assertLogs("core.track1_security", level=logging.WARNING) as logs:
            self.client.post(ANALYZE, json=PAYLOAD, headers={"X-TD-Track1-Key": sent})
            with patch.dict(os.environ, {"TRACK1_INTERNAL_SHARED_SECRET": "short"}):
                self.client.post(ANALYZE, json=PAYLOAD, headers={"X-TD-Track1-Key": "short"})
        output = "\n".join(logs.output)
        for value in (TRACK1_KEY, sent, "short\n", "uid-1", "res-1"):
            self.assertNotIn(value, output)

    def test_track2_gate_is_unaffected(self):
        # Track 1 키만으로는 VLM 경로가 열리지 않고 VLM 게이트의 problem 응답이 유지됩니다.
        response = self.client.post(VLM_PATH, json={}, headers={"X-TD-Track1-Key": TRACK1_KEY})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INTERNAL_AUTH_REQUIRED")
        self.assertTrue(response.headers["content-type"].startswith("application/problem+json"))
        # Track 1 설정이 없어도 VLM 인증을 통과하고 이후 처리는 VLM 경로의 problem 응답을 따릅니다.
        with patch.dict(os.environ, {"VLM_EXTERNAL_CALLS_ENABLED": "false"}, clear=False):
            os.environ.pop("TRACK1_INTERNAL_SHARED_SECRET", None)
            response = self.client.post(VLM_PATH, json={}, headers={"X-TD-Internal-Key": VLM_KEY})
        self.assertTrue(response.headers["content-type"].startswith("application/problem+json"))
        self.assertNotEqual(response.json()["code"], "INTERNAL_AUTH_REQUIRED")
        self.assertNotIn("TRACK1_AUTH_UNCONFIGURED", response.text)


if __name__ == "__main__":
    unittest.main()
