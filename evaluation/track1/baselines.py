"""Offline B0--B2 baselines with a fixed transaction-score policy."""

from __future__ import annotations

from collections import defaultdict
import random
from typing import Iterable

from .models import BaselineOutput, EvaluationRecord, SIGNAL_TYPES, SIGNAL_WEIGHTS


def _scoped_token_index(records: Iterable[EvaluationRecord]) -> dict[tuple[str, str, str, str], list[str]]:
    """Index only within tenant/event/signal scopes, matching the evaluation boundary."""

    index: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    for record in records:
        for signal in SIGNAL_TYPES:
            token = record.signals.get(signal)
            if token is not None:
                index[(record.tenant_id, record.event_id, signal, token)].append(record.ticket_id)
    return index


def _zero_scores(records: Iterable[EvaluationRecord]) -> dict[str, float]:
    return {record.ticket_id: 0.0 for record in records}


def b0_random(records: tuple[EvaluationRecord, ...], seed: int) -> BaselineOutput:
    """B0: deterministic random ranking; its seed is retained in result metadata."""

    generator = random.Random(seed)
    return BaselineOutput(
        name="B0_RANDOM",
        status="COMPLETED",
        scores={record.ticket_id: generator.random() for record in records},
        detail=f"seed={seed}",
    )


def b1_shared_signal_rule(records: tuple[EvaluationRecord, ...]) -> BaselineOutput:
    """B1: same four signals, weighted only when their scoped token is shared."""

    index = _scoped_token_index(records)
    scores = _zero_scores(records)
    for record in records:
        score = 0.0
        for signal, weight in SIGNAL_WEIGHTS.items():
            token = record.signals.get(signal)
            if token is not None and len(index[(record.tenant_id, record.event_id, signal, token)]) >= 2:
                score += weight
        scores[record.ticket_id] = round(score, 6)
    return BaselineOutput(name="B1_SHARED_SIGNAL_RULE", status="COMPLETED", scores=scores)


class _UnionFind:
    def __init__(self, ticket_ids: Iterable[str]) -> None:
        self.parent = {ticket_id: ticket_id for ticket_id in ticket_ids}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: str, second: str) -> None:
        first_root, second_root = self.find(first), self.find(second)
        if first_root != second_root:
            self.parent[max(first_root, second_root)] = min(first_root, second_root)


def b2_wcc(records: tuple[EvaluationRecord, ...]) -> BaselineOutput:
    """B2: local weakly-connected-components baseline, explicitly not Louvain."""

    union_find = _UnionFind(record.ticket_id for record in records)
    for members in _scoped_token_index(records).values():
        for member in members[1:]:
            union_find.union(members[0], member)

    components: dict[str, list[str]] = defaultdict(list)
    for record in records:
        components[union_find.find(record.ticket_id)].append(record.ticket_id)

    scores = _zero_scores(records)
    labels: dict[str, str] = {}
    for root, members in components.items():
        # A singleton has no candidate edge. Components are scored by size only
        # so the baseline does not masquerade as the current Louvain risk score.
        component_score = round(min((len(members) - 1) / 4, 1.0), 6)
        for member in members:
            scores[member] = component_score
            labels[member] = f"wcc-{root}"
    return BaselineOutput(
        name="B2_WCC",
        status="COMPLETED",
        scores=scores,
        cluster_labels=labels,
        detail="Local offline WCC baseline; it is not reported as Louvain.",
    )
