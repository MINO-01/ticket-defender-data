import csv
import tempfile
import unittest
from pathlib import Path

from evaluation.track1.baselines import b0_random, b1_shared_signal_rule, b2_wcc
from evaluation.track1.gds import b3_existing_gds_louvain
from evaluation.track1.ieee import holdout_test_dataset, load_ieee_cis
from evaluation.track1.metrics import evaluate
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

    def test_output_cannot_be_written_inside_repository(self):
        repository = Path(__file__).resolve().parents[1]
        with self.assertRaises(ValueError):
            write_results(repository=repository, output_dir=repository / "evaluation-results", run_name="test", status="COMPLETED", rows=[], metadata={})


if __name__ == "__main__":
    unittest.main()
