"""실모델 평가 체계의 CI 검증. 외부 모델을 호출하지 않고 fixture, 채점기, 실행 게이트, 요청 구성만 확인합니다."""

import base64
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from schemas.listing_context import ListingFields, MAX_LISTING_BASE64_LENGTH, MAX_LISTING_IMAGE_BYTES
from scripts import live_vlm_eval
from services.image_validation import ValidatedImage, validate_image
from services.vlm_service import GeminiProvider, PermanentProviderError


REQUIRED_CATEGORIES = {
    "일반 한국어 티켓", "한국어 재판매 게시물", "공연명 및 좌석이 모두 명확한 이미지", "가격이 만원 단위로 표시된 이미지",
    "일부 가린 좌석", "이미지에 없는 필드", "복수 좌석 후보", "저화질 및 작은 글자", "읽을 수 없는 이미지",
    "이미지 안에 명령문이 포함된 사례",
}


def found(value, quote=None):
    return {"state": "FOUND", "value": value, "evidenceQuote": quote or value, "unknownReason": None, "candidates": []}


class FixtureManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = live_vlm_eval.load_manifest()

    def test_manifest_covers_required_categories_with_verified_digests(self):
        self.assertEqual(REQUIRED_CATEGORIES, {entry["category"] for entry in self.entries})
        self.assertEqual({"image/png", "image/jpeg"}, {entry["mimeType"] for entry in self.entries})
        for entry in self.entries:
            with self.subTest(fixture=entry["id"]):
                encoded = base64.b64encode(entry["data"]).decode("ascii")
                image = validate_image(encoded, entry["mimeType"], max_base64_length=MAX_LISTING_BASE64_LENGTH,
                                       max_bytes=MAX_LISTING_IMAGE_BYTES, expected_sha256=entry["sha256"])
                self.assertEqual((entry["width"], entry["height"]), (image.width, image.height))

    def test_tampered_fixture_is_refused_before_any_use(self):
        with tempfile.TemporaryDirectory() as directory:
            source = live_vlm_eval.FIXTURE_DIR
            target = Path(directory)
            for path in source.iterdir():
                (target / path.name).write_bytes(path.read_bytes())
            first = self.entries[0]["file"]
            (target / first).write_bytes((target / first).read_bytes() + b"\0")
            with self.assertRaises(ValueError):
                live_vlm_eval.load_manifest(target)

    def test_expected_answers_cover_nine_fields_and_valid_states(self):
        for entry in self.entries:
            with self.subTest(fixture=entry["id"]):
                self.assertEqual(set(live_vlm_eval.FIELD_NAMES), set(entry["expected"]))
                self.assertTrue(set(entry["expectedStatus"]) <= {"COMPLETED", "PARTIAL", "UNVERIFIABLE"})
                for spec in entry["expected"].values():
                    self.assertTrue(spec["accept"])
                    self.assertTrue(set(spec["accept"]) <= {"FOUND", "UNKNOWN", "AMBIGUOUS"})
                    if "FOUND" in spec["accept"]:
                        self.assertIn(spec["match"], {"contains", "seat", "exact", "price"})
        by_id = {entry["id"]: entry for entry in self.entries}
        self.assertEqual(["UNVERIFIABLE"], by_id["unreadable_blur"]["expectedStatus"])
        self.assertNotIn("FOUND", by_id["multiple_seat_candidates"]["expected"]["seat"]["accept"])
        self.assertNotIn("FOUND", by_id["seat_partially_masked"]["expected"]["seat"]["accept"])
        self.assertEqual("150000", by_id["price_manwon"]["expected"]["askingPrice"]["amount"])

    def test_fixture_text_has_no_personal_or_ticket_identifiers(self):
        for entry in self.entries:
            text = " ".join(entry["visibleText"])
            with self.subTest(fixture=entry["id"]):
                self.assertIsNone(re.search(r"01[016789]-?\d{3,4}-?\d{4}|@|예매번호|주문번호|\d{8,}", text))


class ScorerTests(unittest.TestCase):
    def test_field_outcomes(self):
        seat = {"accept": ["FOUND"], "match": "seat", "values": ["12"]}
        cases = [
            (seat, found("12번", "A구역 7열 12번"), live_vlm_eval.CORRECT_FOUND),
            (seat, found("13", "13"), live_vlm_eval.WRONG_FOUND),
            (seat, {"state": "UNKNOWN"}, live_vlm_eval.MISSED),
            ({"accept": ["UNKNOWN"]}, found("x"), live_vlm_eval.FALSE_FOUND),
            ({"accept": ["UNKNOWN"]}, {"state": "AMBIGUOUS"}, live_vlm_eval.STATE_MISMATCH),
            ({"accept": ["AMBIGUOUS", "UNKNOWN"]}, {"state": "UNKNOWN"}, live_vlm_eval.ACCEPTED_NON_FOUND),
            ({"accept": ["FOUND"], "match": "price", "amount": "150000"},
             {"state": "FOUND", "value": {"raw": "15만원", "amount": "150000"}}, live_vlm_eval.CORRECT_FOUND),
            ({"accept": ["FOUND"], "match": "price", "amount": "70000"},
             {"state": "FOUND", "value": {"raw": "1원", "amount": "1"}}, live_vlm_eval.WRONG_FOUND),
            ({"accept": ["FOUND"], "match": "contains", "values": ["푸른별 콘서트"]},
             found("2026 푸른별콘서트"), live_vlm_eval.CORRECT_FOUND),
        ]
        for spec, observation, expected in cases:
            with self.subTest(expected=expected, observation=observation):
                self.assertEqual(expected, live_vlm_eval.score_field(spec, observation))

    def test_failed_response_is_not_scored_as_empty_success(self):
        entry = live_vlm_eval.load_manifest()[0]
        result = live_vlm_eval.score_response(entry, 503, {"code": "MODEL_UNAVAILABLE"})
        self.assertEqual("FAILED", result["status"])
        self.assertFalse(result["statusAccepted"])
        summary = live_vlm_eval.summarize([result])
        self.assertEqual(0, summary["httpSuccess"])
        self.assertIsNone(summary["fieldAccuracy"])


class LiveRunGateTests(unittest.TestCase):
    def test_live_run_requires_confirmation_and_key_before_any_call(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(live_vlm_eval, "run_route_evaluation") as route, \
                patch.object(live_vlm_eval.CallMeter, "install") as install:
            output = str(Path(directory) / "report.json")
            self.assertEqual(2, live_vlm_eval.main(["--output", output]))
            self.assertEqual(2, live_vlm_eval.main([live_vlm_eval.CONFIRM_FLAG, "--output", output, "--max-calls", "99"]))
            with patch.object(live_vlm_eval, "_api_key", return_value=""):
                self.assertEqual(3, live_vlm_eval.main([live_vlm_eval.CONFIRM_FLAG, "--output", output]))
            route.assert_not_called()
            install.assert_not_called()
            self.assertFalse(Path(output).exists())


def failed_route_results(entries, _internal_key, meter, calls):
    """외부 호출 없이 경로 평가가 호출 예산을 정확히 calls 회 소진한 상황을 재현합니다."""
    meter.calls.extend({"index": index + 1, "error": "ServerError", "errorCode": 503} for index in range(calls))
    return [live_vlm_eval.score_response(entry, 503, {"code": "MODEL_UNAVAILABLE"}) for entry in entries]


class LiveRunSelectionAndBudgetTests(unittest.TestCase):
    """실제 Gemini 를 호출하지 않도록 키, SDK 계측, 경로 평가를 모두 대체합니다."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "report.json"
        for patcher in (patch.dict(os.environ), patch.object(live_vlm_eval, "_api_key", return_value="synthetic-key"),
                        patch.object(live_vlm_eval.CallMeter, "install"), patch("sys.stdout")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *extra):
        return live_vlm_eval.main([live_vlm_eval.CONFIRM_FLAG, "--output", str(self.output), *extra])

    def test_unknown_only_id_fails_before_any_call(self):
        with patch.object(live_vlm_eval, "run_route_evaluation") as route:
            self.assertEqual(2, self.run_main("--only", "ko_resale_post", "no_such_fixture"))
        route.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_only_without_ids_is_rejected_instead_of_running_all(self):
        with patch.object(live_vlm_eval, "run_route_evaluation") as route, \
                patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            self.run_main("--only")
        self.assertEqual(2, raised.exception.code)
        route.assert_not_called()

    def test_single_and_multiple_only_ids_keep_selection(self):
        for ids in (["price_manwon"], ["ko_resale_post", "price_manwon"]):
            with self.subTest(ids=ids), patch.object(
                    live_vlm_eval, "run_route_evaluation",
                    side_effect=lambda entries, key, meter: failed_route_results(entries, key, meter, 0)) as route:
                self.assertEqual(0, self.run_main("--only", *ids))
                self.assertEqual(ids, [entry["id"] for entry in route.call_args.args[0]])

    def test_probes_skipped_when_route_spent_exact_budget_and_report_is_kept(self):
        with patch.object(live_vlm_eval, "run_route_evaluation",
                          side_effect=lambda entries, key, meter: failed_route_results(entries, key, meter, 3)), \
                patch.object(live_vlm_eval, "run_failure_probes") as probes:
            self.assertEqual(0, self.run_main("--only", "ko_resale_post", "--max-calls", "3", "--failure-probes"))
        probes.assert_not_called()
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual("SKIPPED_INSUFFICIENT_BUDGET", report["failureProbesStatus"])
        self.assertEqual(3, len(report["calls"]))
        self.assertEqual(["ko_resale_post"], [result["id"] for result in report["results"]])

    def test_probes_run_only_with_worst_case_budget(self):
        calls = live_vlm_eval.DEFAULT_MAX_CALLS - live_vlm_eval.FAILURE_PROBE_MAX_CALLS
        with patch.object(live_vlm_eval, "run_route_evaluation",
                          side_effect=lambda entries, key, meter: failed_route_results(entries, key, meter, calls)), \
                patch.object(live_vlm_eval, "run_failure_probes", return_value=[]) as probes:
            self.assertEqual(0, self.run_main("--only", "ko_resale_post", "--failure-probes"))
        probes.assert_called_once()
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual("COMPLETED", report["failureProbesStatus"])


class FailureProbeBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_exceeded_inside_probe_is_recorded_not_raised(self):
        class BudgetProvider:
            def __init__(self, *_args):
                self.calls = 0

            async def generate(self, *_args):
                self.calls += 1
                raise live_vlm_eval.CallBudgetExceeded()

            async def close(self):
                pass

        entry = live_vlm_eval.load_manifest()[0]
        with patch("services.vlm_service.GeminiProvider", BudgetProvider):
            results = await live_vlm_eval.run_failure_probes(entry, "synthetic-key", "fake-model")
        self.assertEqual([{"probe": "permanent_unknown_model", "outcome": "BUDGET_EXHAUSTED"}], results)


class GeminiRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_sends_image_bytes_json_schema_and_no_deprecated_sampling(self):
        captured = {}

        class FakeModels:
            async def generate_content(self, **kwargs):
                captured.update(kwargs)
                raise PermanentProviderError()

        adapter = GeminiProvider.__new__(GeminiProvider)
        adapter.model_name = "fake-model"
        adapter.client = type("Client", (), {"aio": type("Async", (), {"models": FakeModels()})()})()
        data = live_vlm_eval.load_manifest()[0]["data"]
        image = ValidatedImage(data, "image/png", "0" * 64, 1, 1)
        with self.assertRaises(PermanentProviderError):
            await adapter.generate(image, "synthetic prompt", ListingFields)

        config = captured["config"]
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.response_schema)
        schema = config.response_json_schema
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(live_vlm_eval.FIELD_NAMES), set(schema["required"]))
        part = captured["contents"][1]
        self.assertEqual((data, "image/png"), (part.inline_data.data, part.inline_data.mime_type))


if __name__ == "__main__":
    unittest.main()
