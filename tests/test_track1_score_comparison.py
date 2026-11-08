"""score-v1 보존, 합성 원장 v2, 공정 비교의 회귀 테스트입니다."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.capstone_demo import ledger, ledger_v2
from evaluation.capstone_demo.compare import (
    PROXY_ENGINE, MeasuringDriver, compare_ledger, evaluate_clusters, load_request, proxy_v1_clusters,
)
from evaluation.track1.legacy_score_v1 import legacy_clusters_from_communities, legacy_edges
from schemas.dto import Track1AnalysisRequest, Track1Cluster, Track1ClusterMember
from services.track1_scoring import (
    PurchaseLimitPolicy, evaluate_account_quantities, unit_row_quantities,
)

REQUEST_ID = "2136558c-2326-4430-a46d-0793494e9b49"


def t(account, reservation, event="e1", **tokens):
    return {"accountId": account, "reservationNo": reservation, "eventId": event,
            **{key: value * 64 for key, value in tokens.items()}}


def req(tickets):
    return Track1AnalysisRequest.model_validate({"requestId": REQUEST_ID, "tickets": tickets})


class LegacyScoreV1Tests(unittest.TestCase):
    def test_golden_scores_match_main_implementation(self):
        """기대값은 main 40a3d1b의 GraphService로 계산해 고정한 값입니다."""

        request = req([
            t("solo", "r0", paymentHash="a", deviceIdHash="b"),
            t("solo", "r1", paymentHash="a", deviceIdHash="b"),
            t("x", "r2", addressHash="c", ipHash="d"),
            t("y", "r3", addressHash="c", ipHash="d"),
            t("z", "r4", ipHash="d"),
        ])
        records = [
            {"ticketIndex": 0, "communityId": 1}, {"ticketIndex": 1, "communityId": 1},
            {"ticketIndex": 2, "communityId": 2}, {"ticketIndex": 3, "communityId": 2},
            {"ticketIndex": 4, "communityId": 3}, {"ticketIndex": -1, "communityId": 1},
        ]
        clusters = legacy_clusters_from_communities(request, records)
        self.assertEqual(
            sorted((len(c.members), c.risk_score) for c in clusters),
            [(2, 0.2542), (2, 0.6375)],
        )
        self.assertEqual(len(legacy_edges(request)), 9)

    def test_defect_is_preserved_only_in_the_baseline(self):
        clusters = proxy_v1_clusters(req([t("solo", "r0", paymentHash="a"), t("solo", "r1", paymentHash="a")]))
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len({m.account_id for m in clusters[0].members}), 1)


class LedgerV1PreservationTests(unittest.TestCase):
    def test_ledger_v1_generator_is_unchanged_from_main(self):
        # main 40a3d1b의 ledger.py와 tokens.py를 LF로 정규화해 계산한 값입니다.
        self.assertEqual(
            ledger.generator_code_sha256(),
            "c92caa9bc8a2695662793c0b93951c5b2d4ba3ea7cfdb8b7f72310ab8ce2672b",
        )

    def test_v1_output_remains_deterministic(self):
        first, second = ledger.build_dataset(), ledger.build_dataset()
        self.assertEqual(first.files, second.files)
        self.assertEqual(first.row_count, 960)
        self.assertEqual(first.source_version, "capstone-demo-ledger-v1-seed20261108")


class LedgerV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = ledger_v2.build_dataset()
        cls.tickets = json.loads(cls.dataset.files[ledger.TRACK1_FILE])["tickets"]
        cls.answer = json.loads(cls.dataset.files[ledger.ANSWER_FILE])

    def test_is_deterministic_and_distinct_from_v1(self):
        self.assertEqual(ledger_v2.build_dataset().files, self.dataset.files)
        v1 = ledger.build_dataset()
        self.assertNotEqual(v1.source_version, self.dataset.source_version)
        self.assertNotEqual(v1.rows_sha256, self.dataset.rows_sha256)
        self.assertNotEqual(v1.performance_id, self.dataset.performance_id)

    def test_960_rows_and_unique_seats(self):
        self.assertEqual(self.dataset.row_count, 960)
        self.assertEqual(len(self.tickets), 960)
        self.assertEqual(len({ticket["reservationNo"] for ticket in self.tickets}), 960)

    def test_every_account_is_within_two_tickets(self):
        request = req(self.tickets)
        self.assertLessEqual(max(unit_row_quantities(request.tickets).values()), 2)

    def test_a3_and_track1_references_match(self):
        a3 = self.dataset.files[ledger.A3_FILE].decode("ascii")
        refs = {line.split("=", 1)[1] for line in a3.splitlines() if ".reservation-reference=" in line}
        self.assertEqual(refs, {ticket["reservationNo"] for ticket in self.tickets})
        self.assertIn("source-version=capstone-demo-ledger-v2-seed20261108", a3)

    def test_patterns_and_n3_category(self):
        categories = {pattern["patternId"]: pattern["category"] for pattern in self.answer["patterns"]}
        self.assertEqual(set(categories), {"R1", "R2", "R3", "N1", "N2", "N3"})
        self.assertEqual(categories["N3"], "POLICY_AMBIGUOUS_REVIEW")

    def test_no_labels_or_results_in_analysis_input(self):
        raw = self.dataset.files[ledger.TRACK1_FILE].decode("utf-8")
        for forbidden in ("patternId", "riskScore", "clusterId", '"R1"', "N3"):
            self.assertNotIn(forbidden, raw)
        self.assertEqual(
            {key for ticket in self.tickets for key in ticket},
            {"accountId", "reservationNo", "eventId", "paymentHash", "addressHash", "deviceIdHash", "ipHash"},
        )

    def test_quantity_fixture_is_separate_from_ledger(self):
        fixture = json.loads(self.dataset.files[ledger_v2.QUANTITY_FIXTURE_FILE])
        self.assertTrue(fixture["notPartOfLedger"])
        refs = {ticket["reservationNo"] for ticket in fixture["tickets"]}
        self.assertFalse(refs & {ticket["reservationNo"] for ticket in self.tickets})
        request = req(fixture["tickets"])
        event = fixture["tickets"][0]["eventId"]
        result = evaluate_account_quantities(
            unit_row_quantities(request.tickets), {event: PurchaseLimitPolicy(2, "test")}, (event,)
        )[0]
        self.assertEqual({f.account_key[1]: f.valid_quantity for f in result.findings},
                         {"SYNTH-Q-THREE": 3, "SYNTH-Q-FOUR": 4})

    def test_unconfirmed_performance_is_not_final(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v2"
            with self.assertRaises(ValueError):
                ledger_v2.generate(target, require_committed_generator=False)
            ledger_v2.generate(target, require_committed_generator=False, allow_unconfirmed_performance=True)
            result = ledger_v2.verify(target, require_committed_generator=False)
            self.assertEqual(result["status"], "PASS", result)
            self.assertFalse(result["finalForImport"])
            with self.assertRaises(FileExistsError):
                ledger_v2.generate(target, require_committed_generator=False, allow_unconfirmed_performance=True)

    def test_tampered_file_fails_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v2"
            ledger_v2.generate(target, require_committed_generator=False, allow_unconfirmed_performance=True)
            path = target / ledger.TRACK1_FILE
            path.write_bytes(path.read_bytes() + b"\n")
            self.assertEqual(ledger_v2.verify(target, require_committed_generator=False)["status"], "FAIL")


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        target = Path(cls.temp.name)
        for name, content in ledger_v2.build_dataset().files.items():
            (target / name).write_bytes(content)
        cls.directory = target
        cls.result = compare_ledger(target)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def row(self, version):
        return next(r for r in self.result["rows"] if r["engine"] == PROXY_ENGINE and r["scoreVersion"] == version)

    def test_comparison_is_reproducible_apart_from_runtime(self):
        def stable(result):
            return [{k: v for k, v in row.items() if k != "runtimeMs"} for row in result["rows"]]

        self.assertEqual(stable(compare_ledger(self.directory)), stable(self.result))
        self.assertEqual(self.result["metricDefinitionVersion"], "capstone-compare-metrics-v2-fixed-k")

    def test_gds_rows_are_skipped_without_isolation(self):
        gds = [r for r in self.result["rows"] if r["engine"] != PROXY_ENGINE]
        self.assertEqual([r["status"] for r in gds], ["SKIPPED_GDS_ISOLATION"] * 2)

    def test_v2_has_no_single_account_clusters_and_v1_does(self):
        self.assertGreater(self.row("score-v1")["singleAccountClusterCount"], 0)
        self.assertEqual(self.row("score-v2")["singleAccountClusterCount"], 0)
        self.assertEqual(self.row("score-v2")["independentAccountsFlagged"], 0)

    def test_n3_is_always_reported(self):
        for version in ("score-v1", "score-v2"):
            n3 = self.row(version)["patternTable"]["N3"]
            self.assertEqual(n3["accounts"], 6)
            self.assertIn("detectedAccounts", n3)
            self.assertIn("bestRank", n3)
            self.assertTrue(self.row(version)["binaryN3AsNegative"])

    def test_labels_are_not_passed_to_analysis_input(self):
        request, _, _ = load_request(self.directory)
        self.assertNotIn("patternId", request.model_dump_json())

    def test_n3_as_negative_cannot_beat_exclusion(self):
        row = self.row("score-v2")
        for excluded, negative in zip(row["binaryExcludingN3"], row["binaryN3AsNegative"]):
            if excluded["precisionAtK"] is not None and negative["precisionAtK"] is not None:
                self.assertLessEqual(negative["precisionAtK"], excluded["precisionAtK"])

    def test_evaluate_counts_real_ranks(self):
        request = req([t("a", "1", paymentHash="a"), t("b", "2", paymentHash="a"), t("c", "3", paymentHash="c")])
        answer = {"patterns": [{"patternId": "R1", "members": [{"accountId": "a"}, {"accountId": "b"}]}]}
        cluster = Track1Cluster(
            clusterId="c", paymentHashes=["a" * 64], addressHashes=[], deviceIdHashes=[], ipHashes=[], riskScore=0.5,
            members=[Track1ClusterMember(accountId=a, reservationNo=r, eventId="e1") for a, r in (("a", "1"), ("b", "2"))],
        )
        result = evaluate_clusters(request, answer, [cluster])
        self.assertEqual(result["patternTable"]["R1"]["bestRank"], 1)
        # 모집단이 k보다 작으면 고정 k 정밀도는 정의되지 않습니다.
        self.assertIsNone(result["binaryExcludingN3"][0]["precisionAtK"])
        self.assertEqual(result["binaryExcludingN3"][0]["positiveOnlyPrecisionAtK"], 1.0)


class FixedKMetricTests(unittest.TestCase):
    """후보 수와 무관하게 Precision@k 분모가 k로 고정되는지 확인합니다."""

    def setUp(self):
        tickets = [t(f"a{i:02d}", f"r{i}", paymentHash=f"{i:x}"[-1]) for i in range(40)]
        self.request = req(tickets)
        self.answer = {"patterns": [
            {"patternId": "R1", "members": [{"accountId": f"a{i:02d}"} for i in range(4)]},
            {"patternId": "N1", "members": [{"accountId": f"a{i:02d}"} for i in range(4, 6)]},
            {"patternId": "N2", "members": [{"accountId": f"a{i:02d}"} for i in range(6, 8)]},
            {"patternId": "N3", "members": [{"accountId": f"a{i:02d}"} for i in range(8, 10)]},
        ]}

    def clusters(self, accounts):
        return [Track1Cluster(
            clusterId="c", paymentHashes=["a" * 64], addressHashes=[], deviceIdHashes=[], ipHashes=[], riskScore=0.5,
            members=[Track1ClusterMember(accountId=f"a{i:02d}", reservationNo=f"r{i}", eventId="e1") for i in accounts],
        )]

    def at(self, result, mode, k):
        return next(row for row in result[mode] if row["k"] == k)

    def test_precision_denominator_is_k_when_candidates_are_fewer(self):
        result = evaluate_clusters(self.request, self.answer, self.clusters(range(6)))
        row = self.at(result, "binaryExcludingN3", 30)
        self.assertEqual(row["selectedCount"], 6)
        self.assertAlmostEqual(row["precisionAtK"], 4 / 30)
        self.assertAlmostEqual(row["positiveOnlyPrecisionAtK"], 4 / 6)
        self.assertEqual(row["recallAtK"], 1.0)

    def test_fewer_candidates_do_not_inflate_precision(self):
        narrow = evaluate_clusters(self.request, self.answer, self.clusters(range(4)))
        wide = evaluate_clusters(self.request, self.answer, self.clusters(list(range(4)) + list(range(10, 36))))
        self.assertEqual(
            self.at(narrow, "binaryExcludingN3", 30)["precisionAtK"],
            self.at(wide, "binaryExcludingN3", 30)["precisionAtK"],
        )
        self.assertGreater(
            narrow["candidateSummary"]["excludingN3"]["candidatePrecision"],
            wide["candidateSummary"]["excludingN3"]["candidatePrecision"],
        )

    def test_zero_score_positive_is_not_a_hit(self):
        result = evaluate_clusters(self.request, self.answer, self.clusters([10, 11]))
        row = self.at(result, "binaryExcludingN3", 10)
        self.assertEqual(row["precisionAtK"], 0.0)
        self.assertEqual(row["recallAtK"], 0.0)

    def test_n3_modes_differ_only_by_n3_treatment(self):
        result = evaluate_clusters(self.request, self.answer, self.clusters(list(range(4)) + [8, 9]))
        self.assertEqual(result["candidateSummary"]["excludingN3"], {"selectedCount": 4, "candidatePrecision": 1.0})
        self.assertEqual(result["candidateSummary"]["n3AsNegative"], {"selectedCount": 6, "candidatePrecision": 4 / 6})
        self.assertEqual(result["patternTable"]["N3"]["detectedAccounts"], 2)

    def test_normal_shared_rate_uses_all_normal_shared_accounts(self):
        result = evaluate_clusters(self.request, self.answer, self.clusters(range(5)))
        row = self.at(result, "binaryExcludingN3", 10)
        self.assertEqual(row["normalSharedInTop"], 1)
        self.assertEqual(row["normalSharedTopRate"], 1 / 4)
        self.assertEqual(row["normalSharedShareOfSelected"], 1 / 5)


class MeasuringDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_projection_counts_without_touching_other_queries(self):
        seen = []

        class Result:
            async def single(self):
                return {"nodeCount": 3, "relationshipCount": 4}

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def run(self, query, **_):
                seen.append(query)
                return Result()

        class Driver:
            def session(self):
                return Session()

        driver = MeasuringDriver(Driver())
        async with driver.session() as session:
            await session.run("x RETURN graph.nodeCount AS nodeCount", graph_name="g1")
            await session.run("CALL gds.louvain.stream($g)", g="g1")
            await (await session.run("x RETURN graph.nodeCount AS nodeCount", graph_name="g1")).single()
        self.assertIn("graph.relationshipCount", seen[0])
        self.assertEqual(seen[1], "CALL gds.louvain.stream($g)")
        self.assertEqual(driver.projected, ["g1", "g1"])
        self.assertEqual(driver.projection, {"nodeCount": 3, "relationshipCount": 4})


class FrozenLedgerV1HashTests(unittest.TestCase):
    """고정 v1 원장의 SHA-256을 현재 생성기 재현 결과와 대조합니다."""

    def test_v1_bytes_match_frozen_hashes(self):
        dataset = ledger.build_dataset(performance_id="6f53287f-6f6f-564d-8880-bdb728f7f845")
        expected = {
            ledger.A3_FILE: "185dfc3f8c24eef3b61b5d39c3262f6b141029d70666a78f26793bb072053e55",
            ledger.ANSWER_FILE: "a2280cfee3c8975e675707c5df28e709ede454fc0b79f79abb41f6b3b57f90a4",
            ledger.SEATS_FILE: "487499e70a1abe3fb4d350df4fa7cd46970d939ef8d8ce807efab57e1aeaa455",
            ledger.README_FILE: "e236e7ebed2b096dbc3f72b49052f49dba72ce83162c6a356430881220ccb9f2",
            ledger.TRACK1_FILE: "2e58f4152f25fa7b6fb8d3b28c83c0b7d44d27a9e42a8db5da7a7071e9e25a57",
        }
        self.assertEqual({name: sha256(dataset.files[name]).hexdigest() for name in expected}, expected)


if __name__ == "__main__":
    unittest.main()
