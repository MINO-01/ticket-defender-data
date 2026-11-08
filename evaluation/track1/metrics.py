"""Metrics with explicit undefined states; no undefined metric is replaced with zero."""

from __future__ import annotations

from collections import Counter
import math
from typing import Mapping

from .models import BaselineOutput, EvaluationDataset


MetricValue = float | int | str | None


def _rank(scores: Mapping[str, float]) -> list[str]:
    """Deterministic score-desc, ticket-id-asc ranking fixes all score ties."""

    return sorted(scores, key=lambda ticket_id: (-scores[ticket_id], ticket_id))


def _at_fraction(ranking: list[str], fraction: float) -> tuple[list[str], int]:
    k = math.floor(len(ranking) * fraction)
    return ranking[:k], k


def _average_precision(ranking: list[str], labels: Mapping[str, int]) -> float | None:
    positives = sum(labels.get(ticket_id, 0) for ticket_id in ranking)
    if positives == 0:
        return None
    found = 0
    total = 0.0
    for position, ticket_id in enumerate(ranking, start=1):
        if labels.get(ticket_id, 0):
            found += 1
            total += found / position
    return total / positives


def _adjusted_rand_index(first: list[str], second: list[str]) -> float | None:
    if len(first) != len(second) or len(first) < 2:
        return None
    table = Counter(zip(first, second))
    first_sizes, second_sizes = Counter(first), Counter(second)
    choose2 = lambda value: value * (value - 1) / 2
    sum_comb = sum(choose2(value) for value in table.values())
    expected = sum(choose2(value) for value in first_sizes.values()) * sum(choose2(value) for value in second_sizes.values()) / choose2(len(first))
    maximum = (sum(choose2(value) for value in first_sizes.values()) + sum(choose2(value) for value in second_sizes.values())) / 2
    denominator = maximum - expected
    return None if denominator == 0 else (sum_comb - expected) / denominator


def _normalized_mutual_information(first: list[str], second: list[str]) -> float | None:
    if len(first) != len(second) or not first:
        return None
    total = len(first)
    table = Counter(zip(first, second))
    first_sizes, second_sizes = Counter(first), Counter(second)
    mutual_information = sum(
        (count / total) * math.log((count * total) / (first_sizes[left] * second_sizes[right]))
        for (left, right), count in table.items() if count
    )
    entropy = lambda sizes: -sum((count / total) * math.log(count / total) for count in sizes.values() if count)
    denominator = (entropy(first_sizes) + entropy(second_sizes)) / 2
    return None if denominator == 0 else mutual_information / denominator


def evaluate(dataset: EvaluationDataset, output: BaselineOutput) -> dict[str, MetricValue]:
    """Evaluate an output against held-out truth, when truth is available."""

    scores = output.scores
    ranking = _rank(scores)
    candidate_ids = [ticket_id for ticket_id, score in scores.items() if score > 0]
    result: dict[str, MetricValue] = {
        "record_count": len(ranking),
        "candidate_count": len(candidate_ids) if output.status == "COMPLETED" else None,
        "candidate_coverage": (len(candidate_ids) / len(ranking)) if ranking and output.status == "COMPLETED" else None,
        "tie_policy": "score_desc_then_ticket_id_asc",
        "k_at_1pct": math.floor(len(ranking) * 0.01),
        "k_at_5pct": math.floor(len(ranking) * 0.05),
    }
    if output.status != "COMPLETED":
        result.update({
            "precision_at_1pct": None, "recall_at_5pct": None, "lift_at_1pct": None,
            "average_precision": None, "ari": None, "nmi": None,
            "normal_shared_false_candidates": None, "normal_shared_false_candidate_rate": None,
            "metric_status": "N/A_BASELINE_NOT_RUN",
        })
        return result
    if dataset.truth is None:
        result.update({
            "precision_at_1pct": None, "recall_at_5pct": None, "lift_at_1pct": None,
            "average_precision": None, "ari": None, "nmi": None,
            "normal_shared_false_candidates": None, "normal_shared_false_candidate_rate": None,
            "metric_status": "N/A_NO_EVALUATION_LABELS",
        })
        return result

    labels = dataset.truth.investigation_labels
    positives = sum(labels.get(ticket_id, 0) for ticket_id in ranking)
    selected_one, k_one = _at_fraction(ranking, 0.01)
    selected_five, k_five = _at_fraction(ranking, 0.05)
    selected_one_positives = sum(labels.get(ticket_id, 0) for ticket_id in selected_one)
    selected_five_positives = sum(labels.get(ticket_id, 0) for ticket_id in selected_five)
    prevalence = positives / len(ranking) if ranking else None
    precision = selected_one_positives / k_one if k_one else None
    result.update({
        "positive_count": positives,
        "precision_at_1pct": precision,
        "recall_at_5pct": (selected_five_positives / positives) if positives and k_five else None,
        "lift_at_1pct": (precision / prevalence) if precision is not None and prevalence else None,
        "average_precision": _average_precision(ranking, labels),
        "metric_status": "COMPUTED" if positives and k_one and k_five else "N/A_INSUFFICIENT_K_OR_POSITIVES",
    })
    normal_shared = dataset.truth.normal_shared_ticket_ids
    false_candidates = sum(1 for ticket_id in selected_one if ticket_id in normal_shared and not labels.get(ticket_id, 0))
    result["normal_shared_false_candidates"] = false_candidates if k_one else None
    result["normal_shared_false_candidate_rate"] = (false_candidates / len(normal_shared)) if normal_shared and k_one else None
    groups = dataset.truth.relationship_groups
    if output.cluster_labels is None or any(ticket_id not in groups for ticket_id in ranking):
        result.update({"ari": None, "nmi": None})
    else:
        truth_groups = [groups[ticket_id] for ticket_id in ranking]
        output_groups = [output.cluster_labels.get(ticket_id, f"singleton-{ticket_id}") for ticket_id in ranking]
        result["ari"] = _adjusted_rand_index(truth_groups, output_groups)
        result["nmi"] = _normalized_mutual_information(truth_groups, output_groups)
    return result
