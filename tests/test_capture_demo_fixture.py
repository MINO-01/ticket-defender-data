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
        self.assertEqual(len([a for a in accounts if a.startswith("capture-n2-")]), 30)
        self.assertEqual(len([a for a in accounts if a.startswith("capture-r4-")]), 20)
        self.assertEqual(len([r for r in rows if r["account"] == "capture-over"]), 3)
        self.assertEqual(len([r for r in rows if r["account"] == "capture-within"]), 2)

    def test_linked_reservation_is_in_both_ledgers(self):
        reservation = self.manifest["linkedCase"]["reservationReference"]
        rows = capture.build_rows()
        self.assertIn(reservation, {row["reservation"] for row in rows})
        self.assertIn(reservation, {seat["reservation"] for seat in capture.assign_seats(rows)})

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
