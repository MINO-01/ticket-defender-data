import ast
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from pydantic import ValidationError

from evaluation.capstone_demo import capture_template, ledger
from schemas.dto import Track1AnalysisRequest


REPO = Path(__file__).resolve().parents[1]
GENERATED_AT = "2026-11-08T00:00:00Z"
GIT_STATE = {"gitHeadSha": "0" * 40, "generatorCommitted": True}


def properties(content: bytes) -> dict[str, str]:
    return dict(line.split("=", 1) for line in content.decode("ascii").splitlines())


def human_field(text="값"):
    return {"visibility": "VISIBLE", "rawText": text}


def human_observation():
    names = ("eventName", "performanceAt", "platformDisplayName", "askingPrice", "currency",
             "saleText", "zone", "row", "seat")
    return {"recordedBy": "evaluator-1", "recordedAt": GENERATED_AT, "recordedBeforeModelOutput": True,
            **{name: human_field() for name in names}}


class LedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = ledger.build_dataset()
        cls.a3 = properties(cls.dataset.files[ledger.A3_FILE])
        cls.track1 = json.loads(cls.dataset.files[ledger.TRACK1_FILE])["tickets"]
        cls.references = [cls.a3[f"row.{i}.reservation-reference"] for i in range(960)]

    def test_ledger_has_960_unique_rows_and_seats(self):
        self.assertEqual(self.a3["row-count"], "960")
        self.assertEqual(len(set(self.references)), 960)
        seats = Counter((self.a3[f"row.{i}.zone"], self.a3[f"row.{i}.row"], self.a3[f"row.{i}.seat"]) for i in range(960))
        expected = {(z, str(r), str(s)) for z in ("19", "20", "21") for r in range(1, 21) for s in range(1, 17)}
        self.assertEqual(set(seats), expected)
        self.assertEqual(set(seats.values()), {1})
        self.assertEqual(self.a3["fixture-type"], "A3_SYNTHETIC_RESERVATION_SNAPSHOT_V1")
        self.assertEqual(self.a3["complete-snapshot"], "true")
        for reference in self.references:
            self.assertRegex(reference, r"^SYNTHETIC-[A-Z0-9][A-Z0-9_-]{7,117}$")

    def test_a3_and_track1_share_reservation_references(self):
        self.assertEqual(sorted(t["reservationNo"] for t in self.track1), sorted(self.references))

    def test_track1_matches_current_contract_and_token_format(self):
        request = Track1AnalysisRequest.model_validate({
            "requestId": "11111111-1111-4111-8111-111111111111", "tickets": self.track1,
        })
        self.assertEqual(len(request.tickets), 960)
        allowed = {"accountId", "reservationNo", "eventId", "paymentHash", "addressHash", "deviceIdHash", "ipHash"}
        for ticket in self.track1:
            self.assertLessEqual(set(ticket), allowed)
            for key in allowed - {"accountId", "reservationNo", "eventId"}:
                if key in ticket:
                    self.assertRegex(ticket[key], r"^[0-9a-f]{64}$")

    def test_tokens_are_scoped_by_signal(self):
        self.assertNotEqual(ledger._token("payment_hash", "x"), ledger._token("device_id_hash", "x"))
        all_tokens = {key: {t[key] for t in self.track1 if key in t} for key in ("paymentHash", "ipHash")}
        self.assertFalse(all_tokens["paymentHash"] & all_tokens["ipHash"])

    def test_same_seed_is_byte_identical_and_other_seed_differs(self):
        self.assertEqual(ledger.build_dataset().files, self.dataset.files)
        self.assertNotEqual(ledger.build_dataset(seed=7).rows_sha256, self.dataset.rows_sha256)

    def test_answer_key_is_separate_from_inputs(self):
        answer = json.loads(self.dataset.files[ledger.ANSWER_FILE])
        self.assertIs(answer["isAnalysisResult"], False)
        self.assertEqual([p["patternId"] for p in answer["patterns"]], ["R1", "R2", "R3", "N1", "N2", "N3"])
        input_text = (self.dataset.files[ledger.TRACK1_FILE] + self.dataset.files[ledger.A3_FILE]).decode()
        self.assertNotRegex(input_text, r"R[123]\b|N[123]\b|patternId")

    def test_no_output_contains_analysis_results(self):
        for name, content in self.dataset.files.items():
            self.assertNotRegex(content.decode("utf-8"), r"(?i)clusterId|riskScore|communityId", name)

    def test_generator_does_not_depend_on_images_or_models(self):
        tree = ast.parse((REPO / "evaluation/capstone_demo/ledger.py").read_text(encoding="utf-8"))
        imported = {
            (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        banned = re.compile(r"^(PIL|google|openai|requests|httpx|neo4j|services|api)(\.|$)")
        self.assertEqual([name for name in imported if banned.match(name)], [])

    def test_cli_generates_without_images_or_model_keys_outside_repository(self):
        environment = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ledger"
            result = subprocess.run(
                [sys.executable, "-B", "-m", "evaluation.capstone_demo", "generate",
                 "--output-dir", str(output), "--allow-uncommitted-generator"],
                cwd=REPO, env=environment, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sorted(p.name for p in output.iterdir()), sorted((*ledger.OUTPUT_FILES, ledger.MANIFEST_FILE)))
            self.assertEqual(list(output.glob("*.png")) + list(output.glob("*.jp*g")), [])

    def test_output_inside_repository_is_rejected(self):
        with self.assertRaises(ValueError):
            ledger.generate(REPO / "build" / "ledger", require_committed_generator=False)

    def test_frozen_ledger_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ledger"
            ledger.generate(output, require_committed_generator=False)
            with self.assertRaises(FileExistsError):
                ledger.generate(output, require_committed_generator=False)

    def test_uncommitted_generator_cannot_freeze(self):
        original = ledger.generator_git_state
        ledger.generator_git_state = lambda: {"gitHeadSha": None, "generatorCommitted": False}
        self.addCleanup(setattr, ledger, "generator_git_state", original)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(RuntimeError):
            ledger.generate(Path(temporary) / "ledger")

    def test_manifest_hashes_match_and_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ledger"
            manifest_path = ledger.generate(output, require_committed_generator=False)
            manifest = json.loads(manifest_path.read_bytes())
            for name, meta in manifest["files"].items():
                self.assertEqual(sha256((output / name).read_bytes()).hexdigest(), meta["sha256"])
            self.assertEqual(manifest["ledger"]["a3FixtureSha256"], manifest["files"][ledger.A3_FILE]["sha256"])
            self.assertEqual(manifest["track1InputSha256"], manifest["files"][ledger.TRACK1_FILE]["sha256"])
            self.assertEqual(manifest["ledger"]["rowCount"], 960)
            self.assertEqual(manifest["independence"]["externalModelCalls"], 0)
            checks = ledger.verify(output)["checks"]
            self.assertTrue(checks["fileHashesMatchManifest"])
            self.assertTrue(checks["bytesReproducedFromSeed"])

            target = output / ledger.A3_FILE
            target.write_bytes(target.read_bytes().replace(b".zone=19", b".zone=20", 1))
            tampered = ledger.verify(output)
            self.assertEqual(tampered["status"], "FAIL")
            self.assertFalse(tampered["checks"]["fileHashesMatchManifest"])

    def test_performance_override_keeps_rows_and_tokens(self):
        other = ledger.build_dataset(performance_id="44444444-4444-4444-8444-444444444444")
        self.assertEqual(other.rows_sha256, self.dataset.rows_sha256)
        self.assertEqual(other.files[ledger.TRACK1_FILE], self.dataset.files[ledger.TRACK1_FILE])


class CaptureTemplateTests(unittest.TestCase):
    def setUp(self):
        dataset = ledger.build_dataset()
        self.manifest = ledger.build_manifest(dataset, GENERATED_AT, GIT_STATE)
        self.template = capture_template.build_template(self.manifest)

    def test_template_has_four_pending_slots_and_no_results(self):
        self.assertEqual([r["captureId"] for r in self.template["records"]], list(capture_template.CAPTURE_IDS))
        for record in self.template["records"]:
            self.assertEqual(record["status"], "PENDING_CAPTURE")
            for key in ("imageSha256", "capturedAt", "humanObservation", "modelEvaluation", "ledgerComparison"):
                self.assertIsNone(record[key])
        self.assertEqual(self.template["ledgerManifestSha256"], sha256(self.manifest).hexdigest())
        self.assertIs(self.template["isDetectionAccuracyExperiment"], False)

    def _labeled(self):
        record = self.template["records"][0]
        record.update(status="HUMAN_LABELED", imageSha256="a" * 64, capturedAt=GENERATED_AT,
                      humanObservation=human_observation())
        return record

    def test_human_label_is_accepted(self):
        self._labeled()
        capture_template.validate_evaluation(self.template)

    def test_pending_record_cannot_hold_a_result(self):
        self.template["records"][0]["imageSha256"] = "a" * 64
        with self.assertRaises(ValidationError):
            capture_template.validate_evaluation(self.template)

    def test_model_failure_is_not_an_empty_success(self):
        record = self._labeled()
        record.update(status="MODEL_EVALUATED", ledgerComparison={
            "sourceVersion": self.template["ledgerSourceVersion"], "candidateStatus": "ANALYSIS_FAILED",
        }, modelEvaluation={
            "status": "FAILED", "fields": None, "modelName": None, "modelVersion": None, "errorCode": None,
            "durationMs": 10, "failureCount": 1, "retryCount": 0, "externalSdkCallCount": 1,
        })
        with self.assertRaises(ValidationError):
            capture_template.validate_evaluation(self.template)
        record["modelEvaluation"]["errorCode"] = "MODEL_UNAVAILABLE"
        capture_template.validate_evaluation(self.template)

    def test_comparison_must_use_the_frozen_source_version(self):
        record = self._labeled()
        record.update(status="MODEL_EVALUATED", ledgerComparison={
            "sourceVersion": "other-version", "candidateStatus": "NO_MATCH",
        }, modelEvaluation={
            "status": "FAILED", "fields": None, "modelName": None, "modelVersion": None,
            "errorCode": "MODEL_UNAVAILABLE", "durationMs": 10, "failureCount": 1, "retryCount": 0,
            "externalSdkCallCount": 1,
        })
        with self.assertRaises(ValidationError):
            capture_template.validate_evaluation(self.template)

    def test_template_cannot_be_written_inside_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "ledger"
            ledger.generate(source, require_committed_generator=False)
            with self.assertRaises(ValueError):
                capture_template.write_template(source, REPO / "build" / "capture")
            path = capture_template.write_template(source, Path(temporary) / "capture")
            capture_template.validate_evaluation(json.loads(path.read_bytes()))


if __name__ == "__main__":
    unittest.main()
