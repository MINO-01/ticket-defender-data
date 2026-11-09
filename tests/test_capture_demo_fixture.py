"""촬영용 합성 자료의 무결성과 비밀값 비노출을 외부 호출 없이 검증합니다."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load("generate_capture_demo")
env_check = load("check_demo_env")
SECRET = "s" * 48


class CaptureDemoFixtureTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((capture.OUTPUT / capture.MANIFEST).read_text(encoding="utf-8"))

    def test_manifest_and_content_rules_pass(self):
        self.assertEqual(capture.verify(), [])

    def test_fixture_is_marked_synthetic_and_not_final(self):
        self.assertIs(self.manifest["synthetic"], True)
        self.assertIs(self.manifest["finalForImport"], False)

    def test_scenario_composition(self):
        rows = capture.build_rows()
        accounts = {row["account"] for row in rows}
        self.assertEqual(len([a for a in accounts if a.startswith("akmu-n2-")]), 30)
        self.assertEqual(len([a for a in accounts if a.startswith("akmu-r4-")]), 20)
        self.assertEqual(len([r for r in rows if r["account"] == "akmu-over"]), 3)
        self.assertEqual(len([r for r in rows if r["account"] == "akmu-within"]), 2)

    def test_linked_reservation_is_in_both_ledgers(self):
        reservation = self.manifest["linkedCase"]["reservationReference"]
        rows = capture.build_rows()
        self.assertIn(reservation, {row["reservation"] for row in rows})
        self.assertIn(reservation, {seat["reservation"] for seat in capture.assign_seats(rows)})

    def test_listing_a_is_the_linked_r2_reservation_seat(self):
        self.assertEqual(self.manifest["linkedCase"]["account"], "akmu-r2-3")
        self.assertEqual(self.manifest["linkedCase"]["seat"], {"zone": "19", "row": "8", "seat": "12"})
        seat = next(s for s in capture.assign_seats(capture.build_rows()) if s["reservation"] == "akmu-r2-res-3")
        self.assertEqual((seat["zone"], seat["row"], seat["seat"]), ("19", "8", "12"))

    def test_listing_a_price_shows_the_iso_currency_and_keeps_the_amount(self):
        self.assertEqual(capture.LISTING_A_PRICE_TEXT, "200,000원 (KRW)")
        truth = self.manifest["humanTruth"]["listing_A"]
        self.assertEqual(truth["askingPrice"], 200000)
        self.assertEqual(truth["displayPrice"], "200,000원 (KRW)")
        self.assertEqual(truth["currency"], "KRW")

    def test_generator_uses_the_price_text_for_listing_a_only(self):
        source = (SCRIPTS / "generate_capture_demo.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('("판매가", LISTING_A_PRICE_TEXT)'), 1)

    def test_ledgers_are_tracked_with_the_fixture_and_not_ignored_globally(self):
        for name in ("quantity_ledger.csv", "reservation_snapshot.csv"):
            self.assertTrue((capture.OUTPUT / name).is_file(), name)

    def test_performance_matches_the_demo_standard(self):
        performance = self.manifest["performance"]
        self.assertEqual(performance["eventName"], "악동뮤지션 대전 콘서트 (합성 시연)")
        self.assertEqual(performance["performanceAt"], "2026-12-05T18:00:00+09:00")
        self.assertEqual(performance["zones"], ["19", "20", "21"])

    def test_no_uuid_is_fixed_in_ledger(self):
        text = (capture.OUTPUT / "quantity_ledger.csv").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"[0-9a-f]{8}-[0-9a-f]{4}-")


class DemoEnvCheckTests(unittest.TestCase):
    base = {"VLM_ENABLED": "true", "VLM_EXTERNAL_CALLS_ENABLED": "true", "VLM_PROVIDER": "gemini", "VLM_MODEL": "m", "GEMINI_API_KEY": "gemini-value", "VLM_INTERNAL_SHARED_SECRET": SECRET}
    backend = {"TRACK2_LISTING_ANALYSIS_ENABLED": "true", "TRACK2_LISTING_ANALYSIS_URL": "http://127.0.0.1:8000", "TRACK2_LISTING_ANALYSIS_INTERNAL_KEY": SECRET}

    def test_pass_without_printing_values(self):
        lines, passed = env_check.check(self.base, self.backend)
        self.assertTrue(passed)
        self.assertIn("INTERNAL_KEY_MATCH: MATCH", lines)
        self.assertNotIn(SECRET, "\n".join(lines))
        self.assertNotIn("gemini-value", "\n".join(lines))

    def test_missing_key_is_reported(self):
        lines, passed = env_check.check({**self.base, "GEMINI_API_KEY": ""}, None)
        self.assertFalse(passed)
        self.assertIn("GEMINI_API_KEY: GEMINI_KEY_NOT_CONFIGURED", lines)

    def test_mismatch_and_disallowed_url_fail(self):
        lines, passed = env_check.check(self.base, {**self.backend, "TRACK2_LISTING_ANALYSIS_INTERNAL_KEY": "x" * 48, "TRACK2_LISTING_ANALYSIS_URL": "http://example.test"})
        self.assertFalse(passed)
        self.assertIn("INTERNAL_KEY_MATCH: MISMATCH", lines)
        self.assertIn("TRACK2_LISTING_ANALYSIS_URL: MISSING_OR_NOT_ALLOWED", lines)


if __name__ == "__main__":
    unittest.main()
