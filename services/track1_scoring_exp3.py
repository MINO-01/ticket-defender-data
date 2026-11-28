"""순수 exp3 계산과 E1/E2/E3 관계 규칙입니다.

이 모듈은 exp2 구현을 변경하거나 활성화하지 않습니다. 원장 sidecar 검증,
GDS 실행 및 API 선택은 입력 계약이 병합된 뒤 별도로 연결해야 합니다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import math
import unicodedata
from typing import Iterable, Mapping, Sequence

from services.track1_scoring_exp2 import Exp2Account, Exp2Graph, QuantityContext


SCORE_VERSION_EXP3 = "score-v3-exp3-continuity"
PROJECTION_VERSION_EXP3 = "projection-v3-exp3-continuity-ipwindow-2offset"
E1_WINDOW_SECONDS = 90
E1_SINGLE_RELIABILITY = 0.30
E1_REPEAT_RELIABILITY = 0.70
E1_REPEAT_MIN_SEATS = 2
E2_RESOURCE_TYPES = frozenset({
    "phone_hash", "address_hash", "device_id_hash", "refund_account_hash",
})
E2_HARD_HUB_LIMITS = {"device_id_hash": 30, "address_hash": 50}
E2_HUB_RELATIVE_PERCENT = 1
E2_Q_THRESHOLD = 0.95
E3_WINDOW_SECONDS = 600
E3_WINDOW_OFFSETS = (0, 300)
EXP3_SIZE_BASE = 0.6
EXP3_SIZE_SATURATION_LOG2 = 5.0
EXP3_HIGH_BAND = 0.40
EXP3_MEDIUM_BAND = 0.20


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return value.astimezone(timezone.utc)


def _bounded(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be between zero and one")
    return result


def e2_hub_excluded(
    resource_type: str,
    distinct_account_owners: int,
    scope_account_count: int,
) -> bool:
    """E2의 승인된 device/address hard hub 기준만 적용합니다."""

    if resource_type not in E2_RESOURCE_TYPES:
        raise ValueError("unknown E2 resource type")
    if type(distinct_account_owners) is not int or distinct_account_owners < 1:
        raise ValueError("distinct_account_owners must be a positive integer")
    if type(scope_account_count) is not int or scope_account_count < 1:
        raise ValueError("scope_account_count must be a positive integer")
    hard_limit = E2_HARD_HUB_LIMITS.get(resource_type)
    if hard_limit is None:
        return False
    relative = -(-scope_account_count * E2_HUB_RELATIVE_PERCENT // 100)
    return distinct_account_owners >= max(hard_limit, relative)


def normalize_seat_component(value: str | None) -> str | None:
    """NFKC와 양끝 공백만 정규화하고 대소문자 및 선행 0을 보존합니다."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("seat component must be text")
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized or None


@dataclass(frozen=True, slots=True)
class E1Event:
    tenant_id: str
    source_system: str
    performance_id: str
    account_hmac: str = field(repr=False)
    ticket_hmac: str = field(repr=False)
    event_type: str
    reserved_at: datetime | None = None
    cancelled_at: datetime | None = None
    seat_section: str | None = field(default=None, repr=False)
    seat_row: str | None = field(default=None, repr=False)
    seat_number: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("tenant_id", "source_system", "performance_id", "account_hmac", "ticket_hmac"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        if self.event_type not in {"RESERVATION", "CANCELLATION"}:
            raise ValueError("event_type must be RESERVATION or CANCELLATION")
        expected_time = self.reserved_at if self.event_type == "RESERVATION" else self.cancelled_at
        unused_time = self.cancelled_at if self.event_type == "RESERVATION" else self.reserved_at
        if unused_time is not None:
            raise ValueError("E1 event contains the wrong event time field")
        if expected_time is not None:
            if not isinstance(expected_time, datetime):
                raise ValueError("E1 event time must be a datetime")
            _aware_utc(expected_time, "E1 event time")
        for name in ("seat_section", "seat_row", "seat_number"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError("seat component must be text")

    @property
    def event_at(self) -> datetime | None:
        """Reservation uses only reserved_at; cancellation uses only cancelled_at."""

        return self.reserved_at if self.event_type == "RESERVATION" else self.cancelled_at

    @property
    def seat_key(self) -> tuple[str, str, str] | None:
        parts = tuple(normalize_seat_component(value) for value in (
            self.seat_section, self.seat_row, self.seat_number,
        ))
        if any(part is None for part in parts):
            return None
        return parts  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class E1Match:
    tenant_id: str
    source_system: str
    performance_id: str
    cancelled_by_account: str = field(repr=False)
    reserved_by_account: str = field(repr=False)
    cancelled_ticket_hmac: str = field(repr=False)
    reserved_ticket_hmac: str = field(repr=False)
    cancelled_at: datetime
    reserved_at: datetime
    seat_key: tuple[str, str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class E1Relation:
    tenant_id: str
    source_system: str
    performance_id: str
    cancelled_by_account: str = field(repr=False)
    reserved_by_account: str = field(repr=False)
    distinct_seat_count: int
    repeated: bool

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (
            self.tenant_id, self.source_system, self.performance_id,
            self.cancelled_by_account, self.reserved_by_account,
        )):
            raise ValueError("E1 relation identifiers must be non-empty")
        if self.cancelled_by_account == self.reserved_by_account:
            raise ValueError("E1 relation requires two distinct accounts")
        if type(self.distinct_seat_count) is not int or self.distinct_seat_count < 1:
            raise ValueError("E1 relation requires a positive distinct seat count")
        if type(self.repeated) is not bool or self.repeated != (self.distinct_seat_count >= E1_REPEAT_MIN_SEATS):
            raise ValueError("E1 relationship type does not match distinct seat count")


def build_e1_matches(events: Sequence[E1Event]) -> tuple[E1Match, ...]:
    """선택된 회차 안에서 최근 미사용 취소와 예약 이벤트를 결정적으로 1:1 연결합니다."""

    deduplicated: dict[tuple[object, ...], E1Event] = {}
    for event in events:
        seat_key = event.seat_key
        event_time = _aware_utc(event.event_at, "E1 event time") if event.event_at is not None else None
        identity = (
            event.tenant_id, event.source_system, event.performance_id,
            event.ticket_hmac, event.event_type, event_time, seat_key,
        )
        previous = deduplicated.get(identity)
        if previous is not None:
            if previous.account_hmac != event.account_hmac:
                raise ValueError("conflicting E1 duplicate row")
            continue
        deduplicated[identity] = event

    by_seat: dict[tuple[object, ...], list[E1Event]] = defaultdict(list)
    for event in deduplicated.values():
        seat_key = event.seat_key
        if seat_key is None or event.event_at is None:
            continue
        key = (event.tenant_id, event.source_system, event.performance_id, seat_key)
        by_seat[key].append(event)

    matches: list[E1Match] = []
    for key in sorted(by_seat):
        seat_events = by_seat[key]
        reservations = sorted(
            (event for event in seat_events if event.event_type == "RESERVATION"),
            key=lambda item: (_aware_utc(item.event_at, "reserved_at"), item.ticket_hmac),
        )
        cancellations = sorted(
            (event for event in seat_events if event.event_type == "CANCELLATION"),
            key=lambda item: (_aware_utc(item.event_at, "cancelled_at"), item.ticket_hmac),
        )
        used_cancellations: set[int] = set()
        for reservation in reservations:
            reserved_at = _aware_utc(reservation.event_at, "reserved_at")
            eligible: list[tuple[int, E1Event, datetime]] = []
            for index, cancellation in enumerate(cancellations):
                if index in used_cancellations or cancellation.account_hmac == reservation.account_hmac:
                    continue
                cancelled_at = _aware_utc(cancellation.event_at, "cancelled_at")
                delta = reserved_at - cancelled_at
                if 0 < delta.total_seconds() <= E1_WINDOW_SECONDS:
                    eligible.append((index, cancellation, cancelled_at))
            if not eligible:
                continue
            latest_time = max(item[2] for item in eligible)
            chosen_index, cancellation, cancelled_at = min(
                (item for item in eligible if item[2] == latest_time),
                key=lambda item: item[1].ticket_hmac,
            )
            used_cancellations.add(chosen_index)
            matches.append(E1Match(
                tenant_id=reservation.tenant_id,
                source_system=reservation.source_system,
                performance_id=reservation.performance_id,
                cancelled_by_account=cancellation.account_hmac,
                reserved_by_account=reservation.account_hmac,
                cancelled_ticket_hmac=cancellation.ticket_hmac,
                reserved_ticket_hmac=reservation.ticket_hmac,
                cancelled_at=cancelled_at,
                reserved_at=reserved_at,
                seat_key=reservation.seat_key,  # type: ignore[arg-type]
            ))
    return tuple(sorted(matches, key=lambda item: (
        item.tenant_id, item.source_system, item.performance_id,
        item.reserved_at, item.reserved_ticket_hmac,
    )))


def build_e1_relations(events: Sequence[E1Event]) -> tuple[E1Relation, ...]:
    """같은 방향의 회차 관계를 좌석 키 수로 단일/반복 유형에 분류합니다."""

    seats_by_pair: dict[tuple[str, str, str, str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for match in build_e1_matches(events):
        pair = (
            match.tenant_id, match.source_system, match.performance_id,
            match.cancelled_by_account, match.reserved_by_account,
        )
        seats_by_pair[pair].add(match.seat_key)
    return tuple(
        E1Relation(
            tenant_id=key[0], source_system=key[1], performance_id=key[2],
            cancelled_by_account=key[3], reserved_by_account=key[4],
            distinct_seat_count=len(seat_keys),
            repeated=len(seat_keys) >= E1_REPEAT_MIN_SEATS,
        )
        for key, seat_keys in sorted(seats_by_pair.items())
    )


@dataclass(frozen=True, slots=True)
class Exp3Graph:
    """exp2 관계와 E1 후보 edge를 담는 exp3 전용 GDS 투영 입력입니다."""

    accounts: tuple[Exp2Account, ...]
    tokens: tuple[tuple[str, str, str], ...]
    edges: tuple[dict[str, object], ...]
    e1_relations: tuple[E1Relation, ...]


def add_e1_relations_to_exp3_graph(
    base_graph: Exp2Graph,
    relations: Sequence[E1Relation],
    *,
    tenant_id: str,
    source_system: str,
    performance_id: str,
    analysis_scope: str,
) -> Exp3Graph:
    """기존 exp2 투영에 scope 한정 E1 무방향 edge를 한 번씩 추가합니다.

    E1 방향 근거는 ``e1_relations``에 유지합니다. 같은 무방향 계정 쌍에
    양방향 근거가 있으면 projection edge 하나에 더 강한 유형 계수를 씁니다.
    취소 전용 계정도 유효 관계 끝점이면 후보 그래프 노드로 포함합니다.
    """

    if any(not isinstance(value, str) or not value for value in (
        tenant_id, source_system, performance_id, analysis_scope,
    )):
        raise ValueError("exp3 graph scope identifiers must be non-empty")
    if any(account.scope != analysis_scope for account in base_graph.accounts):
        raise ValueError("exp3 base graph contains an unexpected analysis scope")

    selected_by_direction: dict[tuple[str, str], E1Relation] = {}
    for relation in relations:
        if (relation.tenant_id, relation.source_system, relation.performance_id) != (
            tenant_id, source_system, performance_id,
        ):
            continue
        key = (relation.cancelled_by_account, relation.reserved_by_account)
        previous = selected_by_direction.get(key)
        if previous is not None and previous != relation:
            raise ValueError("conflicting E1 relation for the same direction")
        selected_by_direction[key] = relation
    selected_relations = tuple(selected_by_direction[key] for key in sorted(selected_by_direction))

    accounts_by_id = {account.account_id: account for account in base_graph.accounts}
    for relation in selected_relations:
        for account_id in (relation.cancelled_by_account, relation.reserved_by_account):
            accounts_by_id.setdefault(account_id, Exp2Account(
                scope=analysis_scope,
                account_id=account_id,
                reservation_nos=(),
                tokens={},
            ))
    accounts = tuple(accounts_by_id[key] for key in sorted(accounts_by_id))
    account_index = {account.account_id: index for index, account in enumerate(accounts)}
    tokens = base_graph.tokens

    edges: list[dict[str, object]] = []
    old_accounts = base_graph.accounts
    for edge in base_graph.edges:
        old_account_index = edge.get("account_index")
        old_target = edge.get("target")
        if type(old_account_index) is not int or not 0 <= old_account_index < len(old_accounts):
            raise ValueError("invalid exp2 account edge index")
        if type(old_target) is not int:
            raise ValueError("invalid exp2 token edge index")
        token_index = old_target - len(old_accounts)
        if not 0 <= token_index < len(tokens):
            raise ValueError("invalid exp2 token edge index")
        projected_account_index = account_index[old_accounts[old_account_index].account_id]
        projected_token_index = len(accounts) + token_index
        projected = dict(edge)
        projected.update({
            "source": projected_account_index,
            "target": projected_token_index,
            "account_index": projected_account_index,
            "target_account_index": -1,
            "edge_type": "EXP2_SIGNAL",
        })
        edges.append(projected)

    pair_weights: dict[tuple[str, str], float] = {}
    for relation in selected_relations:
        pair = tuple(sorted((relation.cancelled_by_account, relation.reserved_by_account)))
        weight = E1_REPEAT_RELIABILITY if relation.repeated else E1_SINGLE_RELIABILITY
        pair_weights[pair] = max(pair_weights.get(pair, 0.0), weight)
    for (left, right), weight in sorted(pair_weights.items()):
        source_index = account_index[left]
        target_index = account_index[right]
        edges.append({
            "source": source_index,
            "target": target_index,
            "account_index": source_index,
            "target_account_index": target_index,
            "weight": weight,
            "edge_type": "E1",
        })

    return Exp3Graph(accounts, tokens, tuple(edges), selected_relations)


@dataclass(frozen=True, slots=True)
class E1Coverage:
    member_count: int
    single_account_count: int
    repeated_account_count: int
    c1: float
    c2: float
    link: float


def e1_coverage(
    member_accounts: Iterable[str],
    relations: Sequence[E1Relation],
    *,
    tenant_id: str,
    source_system: str,
) -> E1Coverage:
    """반복 유형 우선의 후보 구성원 coverage와 사전등록 `L_E1`을 계산합니다."""

    if not isinstance(tenant_id, str) or not tenant_id or not isinstance(source_system, str) or not source_system:
        raise ValueError("E1 coverage requires a tenant and source system")
    members = frozenset(member_accounts)
    if len(members) < 2 or any(not isinstance(account, str) or not account for account in members):
        raise ValueError("E1 coverage requires at least two distinct accounts")
    single: set[str] = set()
    repeated: set[str] = set()
    for relation in relations:
        if (relation.tenant_id, relation.source_system) != (tenant_id, source_system):
            continue
        endpoints = {relation.cancelled_by_account, relation.reserved_by_account}
        if not endpoints.issubset(members):
            continue
        if relation.repeated:
            repeated.update(endpoints)
        else:
            single.update(endpoints)
    single.difference_update(repeated)
    c1 = len(single) / len(members)
    c2 = len(repeated) / len(members)
    link = 1.0 - (1.0 - E1_SINGLE_RELIABILITY * c1) * (1.0 - E1_REPEAT_RELIABILITY * c2)
    return E1Coverage(len(members), len(single), len(repeated), c1, c2, link)


@dataclass(frozen=True, slots=True)
class PerformanceScope:
    tenant_id: str
    source_system: str
    event_id: str
    performance_id: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (
            self.tenant_id, self.source_system, self.event_id, self.performance_id,
        )):
            raise ValueError("performance scope identifiers must be non-empty")


@dataclass(frozen=True, slots=True)
class E2Candidate:
    candidate_id: str
    analysis_run_id: str
    tenant_id: str
    source_system: str
    active_scopes: tuple[PerformanceScope, ...]
    verified_q: float | None

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (
            self.candidate_id, self.analysis_run_id, self.tenant_id, self.source_system,
        )):
            raise ValueError("candidate scope identifiers must be non-empty")
        if self.verified_q is not None:
            _bounded(self.verified_q, "verified_q")
        if any(scope.tenant_id != self.tenant_id or scope.source_system != self.source_system
               for scope in self.active_scopes):
            raise ValueError("active performance scope crosses tenant or source system")


@dataclass(frozen=True, slots=True)
class E2ResourceObservation:
    scope: PerformanceScope
    account_hmac: str = field(repr=False)
    resource_type: str
    resource_hmac: str = field(repr=False)
    active_record: bool
    retention_verified: bool
    hmac_scope_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.account_hmac, str) or not self.account_hmac:
            raise ValueError("account_hmac must be non-empty")
        if not isinstance(self.resource_hmac, str) or not self.resource_hmac:
            raise ValueError("resource_hmac must be non-empty")
        if self.resource_type not in E2_RESOURCE_TYPES:
            raise ValueError("unknown E2 resource type")


def shared_e2_resource_types(
    candidates: Sequence[E2Candidate],
    candidate_members: Mapping[str, Iterable[str]],
    observations: Sequence[E2ResourceObservation],
    scope_account_counts: Mapping[PerformanceScope, int],
) -> Mapping[tuple[str, str], frozenset[str]]:
    """실제 허용 자원값을 비교해 직접 후보쌍별 공유 유형만 반환합니다.

    관측은 원장 입력 단계에서 가명화된 값이어야 합니다. 취소/환불 행,
    미검증 보유기간, 확인되지 않은 HMAC 범위는 비교에서 제외합니다.
    반환값에는 자원 HMAC이나 계정 참조를 포함하지 않습니다.
    """

    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("candidate IDs must be unique within an analysis run")
    members: dict[str, frozenset[str]] = {}
    for candidate_id in by_id:
        if candidate_id not in candidate_members:
            raise ValueError("candidate member set is missing")
        values = frozenset(candidate_members[candidate_id])
        if any(not isinstance(account, str) or not account for account in values):
            raise ValueError("candidate member reference must be non-empty")
        members[candidate_id] = values

    eligible: list[E2ResourceObservation] = []
    owners_by_scoped_token: dict[tuple[PerformanceScope, str, str], set[str]] = defaultdict(set)
    for observation in observations:
        scope = observation.scope
        if not observation.active_record or not observation.retention_verified or not observation.hmac_scope_verified:
            continue
        if observation.resource_type not in E2_RESOURCE_TYPES:
            continue
        if observation.resource_type in E2_HARD_HUB_LIMITS:
            account_count = scope_account_counts.get(scope)
            if type(account_count) is not int or account_count < 1:
                continue
        eligible.append(observation)
        owners_by_scoped_token[(scope, observation.resource_type, observation.resource_hmac)].add(
            observation.account_hmac,
        )

    candidates_by_token: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for observation in eligible:
        scope = observation.scope
        if observation.resource_type in E2_HARD_HUB_LIMITS:
            account_count = scope_account_counts[scope]
            owner_count = len(owners_by_scoped_token[(scope, observation.resource_type, observation.resource_hmac)])
            if e2_hub_excluded(observation.resource_type, owner_count, account_count):
                continue
        token_key = (
            scope.tenant_id, scope.source_system,
            observation.resource_type, observation.resource_hmac,
        )
        for candidate_id, accounts in members.items():
            candidate = by_id[candidate_id]
            if (candidate.tenant_id, candidate.source_system) != (scope.tenant_id, scope.source_system):
                continue
            if scope not in candidate.active_scopes:
                continue
            if observation.account_hmac in accounts:
                candidates_by_token[token_key].add(candidate_id)

    shared: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (_, _, resource_type, _), candidate_ids in candidates_by_token.items():
        ordered = sorted(candidate_ids)
        for left_index, left_id in enumerate(ordered):
            for right_id in ordered[left_index + 1:]:
                left, right = by_id[left_id], by_id[right_id]
                if (left.analysis_run_id, left.tenant_id, left.source_system) != (
                    right.analysis_run_id, right.tenant_id, right.source_system,
                ):
                    continue
                shared[(left_id, right_id)].add(resource_type)
    return {pair: frozenset(types) for pair, types in shared.items()}


def qualified_e2_neighbors(
    candidates: Sequence[E2Candidate],
    shared_resource_types_by_pair: Mapping[tuple[str, str], Iterable[str]],
) -> Mapping[str, frozenset[str]]:
    """계약 검증 및 hub 제외 후 확인된 직접 자원 공유만 E2 이웃으로 돌려줍니다.

    Map의 각 자원 유형은 실제 동일 HMAC 자원이 두 후보 사이에서 공유되고,
    보유기간, HMAC 범위 및 scope별 hub 검증을 통과했다는 뜻입니다. 이 함수는
    후보를 union하거나 이웃을 통한 전이 연결을 만들지 않습니다.
    """

    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("candidate IDs must be unique within an analysis run")
    neighbors: dict[str, set[str]] = {candidate_id: set() for candidate_id in by_id}
    for pair, resource_types in shared_resource_types_by_pair.items():
        if len(pair) != 2 or pair[0] == pair[1]:
            raise ValueError("E2 pair must contain two distinct candidates")
        left_id, right_id = pair
        if left_id not in by_id or right_id not in by_id:
            raise ValueError("E2 pair references an unknown candidate")
        left, right = by_id[left_id], by_id[right_id]
        if (left.analysis_run_id, left.tenant_id, left.source_system) != (
            right.analysis_run_id, right.tenant_id, right.source_system,
        ):
            raise ValueError("E2 pair crosses analysis run, tenant, or source system")
        types = frozenset(resource_types)
        if not types:
            continue
        if not types.issubset(E2_RESOURCE_TYPES):
            raise ValueError("E2 input contains a non-strong resource type")
        if len(types) >= 2:
            neighbors[left_id].add(right_id)
            neighbors[right_id].add(left_id)
        else:
            if left.verified_q is not None and left.verified_q >= E2_Q_THRESHOLD:
                neighbors[left_id].add(right_id)
            if right.verified_q is not None and right.verified_q >= E2_Q_THRESHOLD:
                neighbors[right_id].add(left_id)
    return {candidate_id: frozenset(sorted(values)) for candidate_id, values in neighbors.items()}


def recurrence_r3(
    candidate_id: str,
    candidates: Sequence[E2Candidate],
    neighbors: Mapping[str, Iterable[str]],
) -> float:
    """고유 active-backed scope와 해당 endpoint가 통과한 직접 E2 이웃만으로 R3를 계산합니다."""

    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if candidate_id not in by_id:
        raise ValueError("unknown candidate")
    candidate = by_id[candidate_id]
    own_scopes = set(candidate.active_scopes)
    if not own_scopes:
        return 0.0
    all_scopes = set(own_scopes)
    for neighbor_id in neighbors.get(candidate_id, ()):
        if neighbor_id == candidate_id or neighbor_id not in by_id:
            raise ValueError("invalid direct E2 neighbor")
        neighbor = by_id[neighbor_id]
        if (candidate.analysis_run_id, candidate.tenant_id, candidate.source_system) != (
            neighbor.analysis_run_id, neighbor.tenant_id, neighbor.source_system,
        ):
            raise ValueError("E2 neighbor crosses analysis run, tenant, or source system")
        all_scopes.update(neighbor.active_scopes)
    return min((len(all_scopes) - 1) / 3.0, 1.0)


def e3_ip_window_tokens(
    analysis_scope: str,
    ip_hmac: str | None,
    reserved_at: datetime | None,
) -> tuple[tuple[int, str], ...]:
    """`reserved_at` 기반 600초 UTC 창 2벌의 가명 토큰을 만듭니다.

    예약 시각이나 IP 가명이 없으면 E3는 사용할 수 없습니다. 구매 시각으로
    대체하지 않습니다. 반환 토큰은 API에 내보내기 위한 원본 IP가 아닙니다.
    """

    if not isinstance(analysis_scope, str) or not analysis_scope:
        raise ValueError("analysis_scope must be non-empty")
    if not ip_hmac or reserved_at is None:
        return ()
    if not isinstance(ip_hmac, str):
        raise ValueError("ip_hmac must be text")
    if not isinstance(reserved_at, datetime) or reserved_at.tzinfo is None or reserved_at.utcoffset() is None:
        return ()
    utc = reserved_at.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    divisor = E3_WINDOW_SECONDS * 1_000_000
    result = []
    for offset in E3_WINDOW_OFFSETS:
        bucket = (micros - offset * 1_000_000) // divisor
        material = f"E3\x00{len(analysis_scope)}:{analysis_scope}\x00{ip_hmac}\x00{offset}\x00{bucket}"
        token = hashlib.sha256(material.encode("utf-8")).hexdigest()
        result.append((offset, token))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Exp3Score:
    score: float
    link: float
    size: float
    behavior: float
    quantity: float
    recurrence: float
    base_link: float
    e1_link: float
    quantity_status: str
    quantity_reason: str | None


def score_exp3(
    member_count: int,
    base_link: float,
    e1: E1Coverage,
    quantity: QuantityContext,
    recurrence: float,
) -> Exp3Score:
    """exp2/E3의 `L_base`, E1 coverage, 검증 Q, R3를 exp3 식으로 결합합니다."""

    if type(member_count) is not int or member_count < 2:
        raise ValueError("score-v3-exp3 requires at least two distinct accounts")
    if e1.member_count != member_count:
        raise ValueError("E1 coverage member count does not match candidate")
    base = _bounded(base_link, "base_link")
    e1_link = _bounded(e1.link, "e1_link")
    r3 = _bounded(recurrence, "recurrence")
    q = _bounded(quantity.value, "quantity")
    link = 1.0 - (1.0 - base) * (1.0 - e1_link)
    size = EXP3_SIZE_BASE + (1.0 - EXP3_SIZE_BASE) * min(
        math.log2(member_count) / EXP3_SIZE_SATURATION_LOG2, 1.0,
    )
    behavior = 0.5 * q + 0.5 * r3
    score = round(link * size * behavior, 4)
    return Exp3Score(
        score=score, link=link, size=size, behavior=behavior, quantity=q,
        recurrence=r3, base_link=base, e1_link=e1_link,
        quantity_status=quantity.status, quantity_reason=quantity.reason,
    )


def priority_band_exp3(score: float) -> str:
    """exp2와 동일하게 고정된 HIGH/MEDIUM 경계를 사용합니다."""

    value = _bounded(score, "score")
    if value >= EXP3_HIGH_BAND:
        return "HIGH"
    return "MEDIUM" if value >= EXP3_MEDIUM_BAND else "LOW"
