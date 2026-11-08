"""Fixed-seed S1--S8 synthetic Track 1 evaluation suite.

The suite contains only clear synthetic identifiers (for example ``acct-s1-01``)
and HMAC tokens derived from symbolic values. It never calls the legacy mock
generator, Faker, or a production data hasher.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Iterable

from .models import EvaluationDataset, EvaluationRecord, EvaluationTruth, SIGNAL_TYPES
from .tokens import hmac_token


DEFAULT_SEED = 20261108


@dataclass
class _ScenarioBuilder:
    scenario: str
    seed: int

    def __post_init__(self) -> None:
        self.records: list[EvaluationRecord] = []
        self.labels: dict[str, int] = {}
        self.groups: dict[str, str] = {}
        self.normal_shared: set[str] = set()
        self.random = random.Random(self.seed)

    def add(
        self,
        role: str,
        index: int,
        *,
        tenant: str = "tenant-synthetic-a",
        event: str | None = None,
        payment: str | None = None,
        address: str | None = None,
        device: str | None = None,
        ip: str | None = None,
        investigated: bool = False,
        group: str | None = None,
        normal_shared: bool = False,
    ) -> str:
        ticket_id = f"txn-{self.scenario}-{role}-{index:03d}"
        event_id = event or f"event-{self.scenario}"
        symbolic = {
            "payment_hash": payment,
            "address_hash": address,
            "device_id_hash": device,
            "ip_hash": ip,
        }
        signals = {
            signal: hmac_token(tenant, signal, (value,)) if value is not None else None
            for signal, value in symbolic.items()
        }
        self.records.append(EvaluationRecord(
            ticket_id=ticket_id,
            account_id=f"acct-{self.scenario}-{role}-{index:03d}",
            reservation_no=f"res-{self.scenario}-{role}-{index:03d}",
            tenant_id=tenant,
            event_id=event_id,
            transaction_dt=1_700_000_000 + len(self.records),
            signals=signals,
        ))
        self.labels[ticket_id] = int(investigated)
        self.groups[ticket_id] = group or f"singleton-{ticket_id}"
        if normal_shared:
            self.normal_shared.add(ticket_id)
        return ticket_id

    def finish(self, description: str) -> EvaluationDataset:
        order = list(self.records)
        self.random.shuffle(order)
        return EvaluationDataset(
            name=self.scenario.upper(),
            records=tuple(order),
            truth=EvaluationTruth(
                investigation_labels=dict(self.labels),
                relationship_groups=dict(self.groups),
                normal_shared_ticket_ids=frozenset(self.normal_shared),
            ),
            metadata={"seed": str(self.seed), "description": description},
        )


def _independent_normals(builder: _ScenarioBuilder, count: int, *, start: int = 1) -> None:
    for index in range(start, start + count):
        builder.add(
            "normal", index, payment=f"payment-normal-{index}", address=f"address-normal-{index}",
            device=f"device-normal-{index}", ip=f"ip-normal-{index}", group=f"normal-{index}",
        )


def _family(builder: _ScenarioBuilder, count: int, *, prefix: str = "family") -> None:
    for index in range(1, count + 1):
        builder.add(
            prefix, index, payment=f"payment-{prefix}-{index}", address=f"address-{prefix}",
            device=f"device-{prefix}-{index}", ip=f"ip-{prefix}", group=prefix, normal_shared=True,
        )


def _target_group(builder: _ScenarioBuilder, count: int, *, prefix: str, signals: dict[str, str | None]) -> None:
    for index in range(1, count + 1):
        builder.add(
            prefix, index, payment=signals.get("payment"), address=signals.get("address"),
            device=signals.get("device"), ip=signals.get("ip"), investigated=True, group=prefix,
        )


def _s1(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s1", seed)
    _target_group(b, 5, prefix="ring", signals={"payment": "pay-ring", "device": "dev-ring", "address": "addr-ring", "ip": "ip-ring"})
    _family(b, 3)
    _independent_normals(b, 12)
    return b.finish("Dense multi-signal investigated group and a normal household sharing address/IP.")


def _s2(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s2", seed)
    _target_group(b, 4, prefix="shared-pay", signals={"payment": "pay-target", "device": "dev-target", "address": None, "ip": "public-net"})
    for index in range(1, 15):
        b.add("public", index, payment=f"pay-public-{index}", address=f"addr-public-{index}", device=f"dev-public-{index}", ip="public-net", group="public-network", normal_shared=True)
    return b.finish("Public-IP counterexample: many normal reservations share one IP.")


def _s3(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s3", seed)
    b.add("chain", 1, payment="pay-a", device="dev-a", ip="ip-a", investigated=True, group="chain")
    b.add("chain", 2, payment="pay-a", device="dev-b", ip="ip-b", investigated=True, group="chain")
    b.add("chain", 3, payment="pay-b", device="dev-b", ip="ip-c", investigated=True, group="chain")
    b.add("chain", 4, payment="pay-b", device="dev-c", ip="ip-c", investigated=True, group="chain")
    _independent_normals(b, 16)
    return b.finish("Indirect multi-hop connection with no single signal common to every target transaction.")


def _s4(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s4", seed)
    b.add("composite", 1, payment="pay-x", address="addr-x", device="dev-1", ip="ip-1", investigated=True, group="composite")
    b.add("composite", 2, payment="pay-x", address="addr-2", device="dev-x", ip="ip-2", investigated=True, group="composite")
    b.add("composite", 3, payment="pay-3", address="addr-x", device="dev-x", ip="ip-3", investigated=True, group="composite")
    b.add("composite", 4, payment="pay-4", address="addr-4", device="dev-x", ip="ip-1", investigated=True, group="composite")
    _family(b, 3, prefix="cohabit")
    _independent_normals(b, 14)
    return b.finish("Composite weak links plus a legitimate cohabiting household.")


def _s5(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s5", seed)
    b.add("missing", 1, payment="pay-missing", device=None, address=None, ip=None, investigated=True, group="missing")
    b.add("missing", 2, payment="pay-missing", device="dev-missing", address=None, ip=None, investigated=True, group="missing")
    b.add("missing", 3, payment=None, device="dev-missing", address=None, ip="ip-missing", investigated=True, group="missing")
    b.add("missing", 4, payment=None, device=None, address=None, ip="ip-missing", investigated=True, group="missing")
    _independent_normals(b, 17)
    return b.finish("Missing-signal target pattern; null means no graph edge, not a value.")


def _s6(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s6", seed)
    _target_group(b, 4, prefix="rare", signals={"payment": "pay-rare", "device": "dev-rare", "address": "addr-rare", "ip": None})
    _family(b, 4, prefix="large-family")
    _independent_normals(b, 92)
    return b.finish("Imbalanced population: a small investigated group among many independent normal reservations.")


def _s7(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s7", seed)
    _target_group(b, 4, prefix="event-target", signals={"payment": "pay-event", "device": "dev-event", "address": None, "ip": None})
    for index in range(1, 4):
        b.add("other-event", index, event="event-s7-other", payment="pay-event", device="dev-event", address=f"addr-other-{index}", ip=f"ip-other-{index}", group="other-event", normal_shared=True)
    _independent_normals(b, 12)
    return b.finish("Event boundary counterexample: same symbolic signals in another event must not join.")


def _s8(seed: int) -> EvaluationDataset:
    b = _ScenarioBuilder("s8", seed)
    for index in range(1, 4):
        b.add("tenant-a", index, tenant="tenant-synthetic-a", payment="shared-cross-tenant", device="shared-cross-tenant", ip="net-a", investigated=True, group="tenant-a-ring")
    for index in range(1, 4):
        b.add("tenant-b", index, tenant="tenant-synthetic-b", payment="shared-cross-tenant", device="shared-cross-tenant", ip="net-b", investigated=False, group="tenant-b-household", normal_shared=True)
    _independent_normals(b, 12)
    return b.finish("Tenant-scoped tokens prevent a synthetic cross-tenant join; this is not a full API tenant-security test.")


def build_synthetic_suite(seed: int = DEFAULT_SEED) -> tuple[EvaluationDataset, ...]:
    """Build S1--S8 with stable input order randomisation and stable labels."""

    builders = (_s1, _s2, _s3, _s4, _s5, _s6, _s7, _s8)
    return tuple(builder(seed + offset) for offset, builder in enumerate(builders, start=1))
