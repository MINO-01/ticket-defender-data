import csv
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from evaluation.track1.baselines import b0_random, b1_shared_signal_rule, b2_wcc
from evaluation.track1 import gds
from evaluation.track1.gds import b3_existing_gds_louvain
from evaluation.track1.ieee import holdout_test_dataset, load_ieee_cis
from evaluation.track1.metrics import evaluate
from evaluation.track1.models import BaselineOutput
from evaluation.track1.results import write_results
from evaluation.track1.synthetic import build_synthetic_suite


class SyntheticSuiteTests(unittest.TestCase):
    def test_s1_to_s8_are_fixed_seed_and_truth_is_separate(self):
        first = build_synthetic_suite()
        second = build_synthetic_suite()
        self.assertEqual([dataset.name for dataset in first], [f"S{number}" for number in range(1, 9)])
        self.assertEqual(first, second)
        for dataset in first:
            self.assertIsNotNone(dataset.truth)
            self.assertEqual(set(dataset.truth.investigation_labels), {record.ticket_id for record in dataset.records})
            for record in dataset.records:
                self.assertFalse(hasattr(record, "is_fraud"))
                self.assertTrue(record.ticket_id.startswith("txn-s"))
                self.assertNotRegex(record.account_id, r"\d{10,}")
                self.assertNotRegex(record.reservation_no, r"\d{10,}")

    def test_tenant_scoped_tokens_do_not_cross_join(self):
        s8 = build_synthetic_suite()[-1]
        target = next(record for record in s8.records if "tenant-a" in record.ticket_id)
        other_tenant = next(record for record in s8.records if "tenant-b" in record.ticket_id)
        self.assertNotEqual(target.signals["payment_hash"], other_tenant.signals["payment_hash"])
        self.assertNotEqual(target.signals["device_id_hash"], other_tenant.signals["device_id_hash"])

    def test_baselines_share_input_and_gds_is_explicitly_skipped(self):
        dataset = build_synthetic_suite()[0]
        outputs = (b0_random(dataset.records, 7), b1_shared_signal_rule(dataset.records), b2_wcc(dataset.records))
        expected_ids = {record.ticket_id for record in dataset.records}
        for output in outputs:
            self.assertEqual(set(output.scores), expected_ids)
        skipped = b3_existing_gds_louvain(dataset.records, uri=None, password=None)
        self.assertEqual(skipped.status, "SKIPPED_GDS_ISOLATION")
        self.assertEqual(evaluate(dataset, skipped)["metric_status"], "N/A_BASELINE_NOT_RUN")
        result = evaluate(dataset, outputs[1])
        self.assertEqual(result["k_at_1pct"], 0)
        self.assertIsNone(result["precision_at_1pct"])
        self.assertEqual(result["metric_status"], "N/A_INSUFFICIENT_K_OR_POSITIVES")


class IeeeAdapterTests(unittest.TestCase):
    def _write_csvs(self, directory: Path) -> None:
        transaction_columns = ["TransactionID", "TransactionDT", "isFraud", "card1", "card2", "addr1", "addr2"]
        identity_columns = ["TransactionID", "DeviceType", "DeviceInfo"]
        with (directory / "train_transaction.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=transaction_columns)
            writer.writeheader()
            for index in range(20):
                writer.writerow({
                    "TransactionID": str(1000 + index), "TransactionDT": str(index * 60),
                    "isFraud": str(int(index in {17, 19})), "card1": str(index % 3),
                    "card2": "10", "addr1": str(index % 2), "addr2": "",
                })
        with (directory / "train_identity.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=identity_columns)
            writer.writeheader()
            writer.writerow({"TransactionID": "1017", "DeviceType": "desktop", "DeviceInfo": "browser-x"})

    def test_left_join_time_split_and_labels_are_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self._write_csvs(directory)
            dataset, split = load_ieee_cis(directory, max_rows=20)
        self.assertEqual(len(dataset.records), 20)
        self.assertEqual(list(split.values()).count("train"), 14)
        self.assertEqual(list(split.values()).count("validation"), 3)
        self.assertEqual(list(split.values()).count("test"), 3)
        self.assertTrue(all(record.signals["ip_hash"] is None for record in dataset.records))
        self.assertTrue(all("isFraud" not in record.signals for record in dataset.records))
        heldout = holdout_test_dataset(dataset, split)
        self.assertEqual(len(heldout.records), 3)
        self.assertEqual(heldout.truth.investigation_labels["ieee-1019"], 1)
        # IEEE-CIS supplies transaction labels, not a relationship-group truth.
        # B2 still emits cluster labels, so this must not attempt an ARI/NMI lookup.
        evaluated = evaluate(heldout, b2_wcc(heldout.records))
        self.assertIsNone(evaluated["ari"])
        self.assertIsNone(evaluated["nmi"])
        self.assertIsNotNone(evaluated["average_precision"])
        self.assertIsNotNone(evaluated["candidate_coverage"])

    def test_output_cannot_be_written_inside_repository(self):
        repository = Path(__file__).resolve().parents[1]
        with self.assertRaises(ValueError):
            write_results(repository=repository, output_dir=repository / "evaluation-results", run_name="test", status="COMPLETED", rows=[], metadata={})


class GdsIsolationTests(unittest.TestCase):
    def setUp(self):
        self.records = build_synthetic_suite()[0].records

    def test_missing_uri_or_password_skips_without_louvain_runner(self):
        with patch("evaluation.track1.gds._run_existing_louvain", new_callable=AsyncMock) as runner:
            self.assertEqual(b3_existing_gds_louvain(self.records, uri=None, password="password").status, "SKIPPED_GDS_ISOLATION")
            self.assertEqual(b3_existing_gds_louvain(self.records, uri="bolt://127.0.0.1:9000", password=None).status, "SKIPPED_GDS_ISOLATION")
            runner.assert_not_called()

    def test_remote_and_default_port_are_blocked_before_docker_or_louvain(self):
        with patch("evaluation.track1.gds._docker_output") as docker, patch(
            "evaluation.track1.gds._run_existing_louvain", new_callable=AsyncMock
        ) as runner:
            for uri in ("bolt://db.example.test:9000", "bolt://localhost:7687", "bolt://127.0.0.1:7687"):
                output = b3_existing_gds_louvain(self.records, uri=uri, password="password")
                self.assertEqual(output.status, "SKIPPED_GDS_ISOLATION")
            docker.assert_not_called()
            runner.assert_not_called()

    def test_unverified_container_skips_before_louvain_runner(self):
        with patch("evaluation.track1.gds.verify_isolated_gds_target", return_value="NO_VERIFIED_GDS_CONTAINER"), patch(
            "evaluation.track1.gds._run_existing_louvain", new_callable=AsyncMock
        ) as runner:
            output = b3_existing_gds_louvain(self.records, uri="bolt://127.0.0.1:19001", password="password")
            self.assertEqual(output.status, "SKIPPED_GDS_ISOLATION")
            self.assertIn("NO_VERIFIED_GDS_CONTAINER", output.detail)
            runner.assert_not_called()

    def test_verified_compose_container_keeps_existing_louvain_path(self):
        expected = BaselineOutput("B3_GDS_LOUVAIN", "COMPLETED", {record.ticket_id: 0.0 for record in self.records})
        with patch("evaluation.track1.gds.verify_isolated_gds_target", return_value=None), patch(
            "evaluation.track1.gds._run_existing_louvain", new_callable=AsyncMock, return_value=expected
        ) as runner:
            output = b3_existing_gds_louvain(self.records, uri="bolt://127.0.0.1:19001", password="password")
            self.assertIs(output, expected)
            runner.assert_awaited_once_with(self.records, "bolt://127.0.0.1:19001", "password")

    def test_verifier_accepts_only_matching_compose_metadata(self):
        container = {
            "Config": {
                "Labels": {"com.docker.compose.project": "tdissue19gds0123456789", "com.docker.compose.service": "neo4j"},
                "Image": "neo4j:5.12.0",
            },
            "Mounts": [{
                "Type": "bind", "Source": "C:/temp/td-issue19-gds-2.6.6.jar",
                "Destination": "/plugins/graph-data-science.jar", "RW": False,
            }],
            "NetworkSettings": {"Ports": {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19001"}]}},
        }
        with patch("evaluation.track1.gds._docker_output", side_effect=["container-id\n", json.dumps([container])]):
            self.assertIsNone(gds.verify_isolated_gds_target("bolt://127.0.0.1:19001"))

    def test_verifier_rejects_near_matching_compose_metadata(self):
        matching_container = {
            "Config": {
                "Labels": {"com.docker.compose.project": "tdissue19gds0123456789", "com.docker.compose.service": "neo4j"},
                "Image": "neo4j:5.12.0",
            },
            "Mounts": [{
                "Type": "bind", "Source": "C:/temp/td-issue19-gds-2.6.6.jar",
                "Destination": "/plugins/graph-data-science.jar", "RW": False,
            }],
            "NetworkSettings": {"Ports": {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19001"}]}},
        }
        cases = []
        read_write_mount = copy.deepcopy(matching_container)
        read_write_mount["Mounts"][0]["RW"] = True
        cases.append(("read_write_jar", [read_write_mount]))
        wrong_project = copy.deepcopy(matching_container)
        wrong_project["Config"]["Labels"]["com.docker.compose.project"] = "ticket-fraud-neo4j"
        cases.append(("wrong_project", [wrong_project]))
        wrong_image = copy.deepcopy(matching_container)
        wrong_image["Config"]["Image"] = "neo4j:5.13.0"
        cases.append(("wrong_image", [wrong_image]))
        wrong_port = copy.deepcopy(matching_container)
        wrong_port["NetworkSettings"]["Ports"]["7687/tcp"][0]["HostPort"] = "19002"
        cases.append(("wrong_port", [wrong_port]))
        cases.append(("multiple_matches", [copy.deepcopy(matching_container), copy.deepcopy(matching_container)]))

        for name, containers in cases:
            with self.subTest(name=name):
                container_ids = "\n".join(f"container-{index}" for index in range(len(containers))) + "\n"
                inspect = json.dumps(containers)
                with patch("evaluation.track1.gds._docker_output", side_effect=[container_ids, inspect]):
                    self.assertEqual(
                        gds.verify_isolated_gds_target("bolt://127.0.0.1:19001"),
                        "NO_VERIFIED_GDS_CONTAINER",
                    )
                with patch("evaluation.track1.gds._docker_output", side_effect=[container_ids, inspect]), patch(
                    "evaluation.track1.gds._run_existing_louvain", new_callable=AsyncMock
                ) as runner:
                    output = b3_existing_gds_louvain(
                        self.records, uri="bolt://127.0.0.1:19001", password="password"
                    )
                    self.assertEqual(output.status, "SKIPPED_GDS_ISOLATION")
                    runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
