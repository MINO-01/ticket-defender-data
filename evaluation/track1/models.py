"""Data structures for offline Track 1 evaluation.

Ground truth intentionally lives outside of ``EvaluationRecord`` so an algorithm
cannot receive the investigation label as an input feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


SIGNAL_TYPES = ("payment_hash", "address_hash", "device_id_hash", "ip_hash")
SIGNAL_WEIGHTS = {
    "payment_hash": 0.35,
    "device_id_hash": 0.25,
    "address_hash": 0.15,
    "ip_hash": 0.10,
}


@dataclass(frozen=True)
class EvaluationRecord:
    """One non-PII reservation-like input used by a baseline."""

    ticket_id: str
    account_id: str
    reservation_no: str
    tenant_id: str
    event_id: str
    transaction_dt: int
    signals: Mapping[str, str | None]

    @property
    def key(self) -> tuple[str, str, str]:
        return self.account_id, self.reservation_no, self.event_id

    @property
    def has_signal(self) -> bool:
        return any(self.signals.get(name) is not None for name in SIGNAL_TYPES)


@dataclass(frozen=True)
class EvaluationTruth:
    """Held separately from inputs and passed only to metric calculation."""

    investigation_labels: Mapping[str, int]
    relationship_groups: Mapping[str, str]
    normal_shared_ticket_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EvaluationDataset:
    name: str
    records: tuple[EvaluationRecord, ...]
    truth: EvaluationTruth | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BaselineOutput:
    name: str
    status: str
    scores: Mapping[str, float]
    cluster_labels: Mapping[str, str] | None = None
    detail: str | None = None
    algorithm_version: str | None = None
