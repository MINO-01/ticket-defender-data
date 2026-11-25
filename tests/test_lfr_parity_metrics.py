import pytest

from evaluation.track1.lfr_parity import adjusted_rand_index, normalized_mutual_information


def test_partition_metrics_are_perfect_for_identical_partition_labels():
    truth = ["a", "a", "b", "b", "c", "c"]
    predicted = ["x", "x", "y", "y", "z", "z"]

    assert adjusted_rand_index(truth, predicted) == pytest.approx(1.0)
    assert normalized_mutual_information(truth, predicted) == pytest.approx(1.0)


def test_partition_metrics_are_label_permutation_invariant():
    truth = ["a", "a", "a", "b", "b", "c", "c", "c"]
    predicted = ["u", "u", "v", "v", "v", "w", "w", "x"]
    permuted = ["three" if value == "u" else "one" if value == "v" else "four" if value == "w" else "two" for value in predicted]

    assert adjusted_rand_index(truth, predicted) == pytest.approx(adjusted_rand_index(truth, permuted))
    assert normalized_mutual_information(truth, predicted) == pytest.approx(normalized_mutual_information(truth, permuted))


def test_partition_metrics_reject_mismatched_or_empty_inputs():
    with pytest.raises(ValueError):
        adjusted_rand_index([], [])
    with pytest.raises(ValueError):
        normalized_mutual_information(["a"], [])
