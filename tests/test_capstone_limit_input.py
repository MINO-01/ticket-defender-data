"""시연용 v2 분석 입력의 합성 전제 분리와 격리 GDS 스크립트 조건을 검증합니다."""

import json
from pathlib import Path
import tempfile
import unittest

from evaluation.capstone_demo import ledger, ledger_v2
from evaluation.capstone_demo.limit_input_v2 import (
    LIMIT_INPUT_FILE, LIMIT_INPUT_MANIFEST_FILE, RegisteredPolicy, build_limit_input, write_limit_input,
)
from schemas.dto import Track1LimitAnalysisRequest
from services.track1_scoring import purchase_limit_evaluations

INTERNAL = "11111111-1111-4111-8111-111111111111"
PURCHASED = "2026-11-02T10:00:00Z"
AS_OF = "2026-11-08T10:00:00Z"
POLICY = RegisteredPolicy(2, "demo-r1", "2026-11-01T00:00:00Z")


class LimitInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.ledger_dir = Path(cls.temp.name) / "ledger"
        ledger_v2.generate(cls.ledger_dir, require_committed_generator=False, allow_unconfirmed_performance=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def build(self, policy=POLICY):
        return build_limit_input(
            self.ledger_dir, internal_event_id=INTERNAL, synthetic_purchased_at=PURCHASED, quantity_as_of=AS_OF,
            policy=policy, require_committed_generator=False, allow_unconfirmed_ledger=True,
        )

    def test_unconfirmed_ledger_is_refused_by_default(self):
        with self.assertRaises(ValueError):
            build_limit_input(
                self.ledger_dir, internal_event_id=INTERNAL, synthetic_purchased_at=PURCHASED, quantity_as_of=AS_OF,
                policy=POLICY, require_committed_generator=False,
            )

    def test_input_matches_ledger_references_and_contract(self):
        input_bytes, _ = self.build()
        payload = json.loads(input_bytes)
        request = Track1LimitAnalysisRequest.model_validate(payload)
        source = json.loads((self.ledger_dir / ledger.TRACK1_FILE).read_text(encoding="utf-8"))["tickets"]
        self.assertEqual({t["reservationNo"] for t in payload["tickets"]}, {t["reservationNo"] for t in source})
        self.assertEqual(len(request.tickets), 960)
        self.assertEqual({t.effective_ticket_quantity for t in request.tickets}, {1})

    def test_two_ticket_ledger_has_no_limit_finding(self):
        request = Track1LimitAnalysisRequest.model_validate_json(self.build()[0])
        result = purchase_limit_evaluations(request)[0]
        self.assertEqual(result.status, "EVALUATED")
        self.assertEqual(result.account_limit_findings, [])

    def test_without_registered_policy_is_not_confirmed(self):
        request = Track1LimitAnalysisRequest.model_validate_json(self.build(policy=None)[0])
        self.assertEqual(purchase_limit_evaluations(request)[0].status, "POLICY_NOT_CONFIRMED")

    def test_manifest_states_synthetic_assumptions_and_hashes(self):
        input_bytes, manifest_bytes = self.build()
        manifest = json.loads(manifest_bytes)
        self.assertFalse(manifest["syntheticAssumptions"]["purchasedAt"]["fromLedger"])
        self.assertFalse(manifest["finalForDemo"])
        self.assertFalse(manifest["independence"]["readsAnswerKey"])
        self.assertEqual(len(manifest["inputSha256"]), 64)
        self.assertEqual(self.build(), (input_bytes, manifest_bytes))

    def test_no_answer_labels_in_input(self):
        raw = self.build()[0].decode("utf-8")
        for forbidden in ("patternId", "riskScore", "clusterId", "R1", "N3", "POLICY_AMBIGUOUS"):
            self.assertNotIn(forbidden, raw)

    def test_writer_never_overwrites_or_touches_ledger(self):
        before = {p.name: p.read_bytes() for p in self.ledger_dir.iterdir()}
        options = dict(
            internal_event_id=INTERNAL, synthetic_purchased_at=PURCHASED, quantity_as_of=AS_OF, policy=POLICY,
            require_committed_generator=False, allow_unconfirmed_ledger=True,
        )
        target = Path(self.temp.name) / "input"
        write_limit_input(self.ledger_dir, target, **options)
        self.assertEqual({p.name for p in target.iterdir()}, {LIMIT_INPUT_FILE, LIMIT_INPUT_MANIFEST_FILE})
        with self.assertRaises(FileExistsError):
            write_limit_input(self.ledger_dir, target, **options)
        with self.assertRaises(ValueError):
            write_limit_input(self.ledger_dir, self.ledger_dir / "nested", **options)
        self.assertEqual({p.name: p.read_bytes() for p in self.ledger_dir.iterdir()}, before)


class GdsScriptGuardTests(unittest.TestCase):
    """격리 스크립트가 해당 프로젝트만 정리하고 무결성을 먼저 확인하는지 봅니다."""

    @classmethod
    def setUpClass(cls):
        cls.text = (Path(__file__).parent / "run_gds_integration.ps1").read_text(encoding="utf-8")

    def test_cleanup_is_scoped_to_generated_project_and_removes_its_volumes(self):
        self.assertIn("docker compose -p $project -f $composeFile down -v --timeout 15", self.text)
        self.assertIn("$project = 'tdissue19gds'", self.text)

    def test_integrity_and_python_checks_precede_docker(self):
        first_docker = self.text.index("docker compose")
        for guard in ("ExpectedJarSha256", "pyvenv.cfg", "Get-FileHash"):
            self.assertLess(self.text.index(guard), first_docker)

    def test_no_pull_and_no_system_volume_commands(self):
        self.assertIn("--pull never", self.text)
        self.assertNotIn("volume rm", self.text)
        self.assertNotIn("prune", self.text)


if __name__ == "__main__":
    unittest.main()
