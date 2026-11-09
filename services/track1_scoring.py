"""Track 1 계정 단위 관계 계산과 score-v2를 담당하는 순수 모듈입니다.

Neo4j에 의존하지 않으며 입력의 예매 목록만으로 결정적으로 계산합니다.
가중치와 허브 임계값은 버전이 있는 실험 파라미터이고 출시 기준이 아닙니다.
점수는 조사 우선순위이며 부정거래 확률이나 동일인 확정 결과가 아닙니다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from typing import Iterable, Mapping, Protocol, Sequence

from schemas.dto import (
    Track1AccountLimitFinding, Track1AnalysisRequest, Track1Cluster, Track1ClusterMember,
    Track1LimitAnalysisRequest, Track1PurchaseLimitEvaluation, Track1ReservationQuantity,
)


PROJECTION_VERSION = "projection-v2-account"
SCORE_VERSION = "score-v2-exp1"

SIGNALS = ("payment_hash", "device_id_hash", "address_hash", "ip_hash")

AccountKey = tuple[str, str]
TokenKey = tuple[str, str, str]


class GraphResultInvalid(Exception):
    pass


class Track1Ticket(Protocol):
    account_id: str
    reservation_no: str
    event_id: str
    payment_hash: str | None
    device_id_hash: str | None
    address_hash: str | None
    ip_hash: str | None

    @property
    def key(self) -> tuple[str, str, str]: ...

    @property
    def analysis_scope(self) -> str: ...


@dataclass(frozen=True)
class ScoreParameters:
    """실험 파라미터입니다. 값을 바꾸면 SCORE_VERSION도 새로 부여해야 합니다."""

    signal_weights: Mapping[str, float] = field(default_factory=lambda: {
        "payment_hash": 0.45, "device_id_hash": 0.40, "address_hash": 0.15, "ip_hash": 0.05,
    })
    # 이 계정 수를 초과하면 허브입니다. 지정되지 않은 신호는 허브 판정을 하지 않습니다.
    hub_account_limits: Mapping[str, int] = field(default_factory=lambda: {
        "ip_hash": 8, "address_hash": 6,
    })
    size_saturation_accounts: int = 5
    size_base: float = 0.75
    single_signal_type_factor: float = 0.8
    high_band: float = 0.6
    medium_band: float = 0.3

    @property
    def weight_sum(self) -> float:
        return sum(self.signal_weights.values())


DEFAULT_PARAMETERS = ScoreParameters()


@dataclass(frozen=True)
class AccountAggregate:
    """한 공연 범위의 계정 1개와 그 계정의 전체 예매, 중복 제거된 토큰입니다."""

    key: AccountKey
    tickets: tuple[Track1Ticket, ...]
    tokens: Mapping[str, frozenset[str]]

    @property
    def ticket_count(self) -> int:
        return len(self.tickets)


@dataclass(frozen=True)
class AccountTokenGraph:
    """GDS에 투영할 계정과 토큰입니다. 실제 공유 관계가 없는 계정은 포함하지 않습니다."""

    accounts: tuple[AccountAggregate, ...]
    tokens: tuple[TokenKey, ...]
    edges: tuple[dict, ...]
    excluded_hubs: tuple[TokenKey, ...]

    @property
    def node_count(self) -> int:
        return len(self.accounts) + len(self.tokens)


def aggregate_accounts(tickets: Iterable[Track1Ticket]) -> dict[AccountKey, AccountAggregate]:
    """같은 분석 범위의 같은 계정을 하나로 묶습니다. 범위가 다르면 별도 계정으로 다룹니다."""

    grouped: dict[AccountKey, list[Track1Ticket]] = defaultdict(list)
    for ticket in tickets:
        grouped[(ticket.analysis_scope, ticket.account_id)].append(ticket)
    return {
        key: AccountAggregate(
            key=key,
            tickets=tuple(sorted(members, key=lambda t: t.reservation_no)),
            tokens={
                signal: frozenset(
                    token for t in members if (token := getattr(t, signal)) is not None
                )
                for signal in SIGNALS
            },
        )
        for key, members in grouped.items()
    }


def token_account_index(accounts: Mapping[AccountKey, AccountAggregate]) -> dict[TokenKey, set[AccountKey]]:
    """토큰별 구별 계정 집합입니다. 같은 계정의 반복 사용은 한 번만 셉니다."""

    index: dict[TokenKey, set[AccountKey]] = defaultdict(set)
    for key, account in accounts.items():
        for signal, tokens in account.tokens.items():
            for token in tokens:
                index[(key[0], signal, token)].add(key)
    return index


def is_hub(signal: str, distinct_accounts: int, parameters: ScoreParameters = DEFAULT_PARAMETERS) -> bool:
    limit = parameters.hub_account_limits.get(signal)
    return limit is not None and distinct_accounts > limit


def build_account_token_graph(
    tickets: Sequence[Track1Ticket], parameters: ScoreParameters = DEFAULT_PARAMETERS
) -> AccountTokenGraph:
    """구별 계정 2개 이상이 공유하고 허브가 아닌 토큰만 간선으로 만듭니다.

    입력 순서가 달라도 같은 그래프가 되도록 계정과 토큰을 정렬합니다.
    """

    accounts = aggregate_accounts(tickets)
    index = token_account_index(accounts)
    shared = sorted(key for key, owners in index.items() if len(owners) >= 2)
    excluded = tuple(key for key in shared if is_hub(key[1], len(index[key]), parameters))
    projected = [key for key in shared if key not in set(excluded)]
    connected = sorted({owner for key in projected for owner in index[key]})
    account_index = {key: position for position, key in enumerate(connected)}
    token_index = {key: len(connected) + position for position, key in enumerate(projected)}
    edges = tuple(
        {
            "source": account_index[owner],
            "target": token_index[token],
            "account_index": account_index[owner],
            "weight": parameters.signal_weights[token[1]],
        }
        for token in projected
        for owner in sorted(index[token])
    )
    return AccountTokenGraph(
        accounts=tuple(accounts[key] for key in connected),
        tokens=tuple(projected),
        edges=edges,
        excluded_hubs=excluded,
    )


def community_shared_tokens(
    members: Sequence[AccountAggregate], graph: AccountTokenGraph
) -> dict[str, list[str]]:
    """군집 내부의 구별 계정 2개 이상이 실제로 공유한 투영 토큰만 근거로 돌려줍니다."""

    event_id = members[0].key[0]
    projected = set(graph.tokens)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for member in members:
        for signal, tokens in member.tokens.items():
            for token in tokens:
                if (event_id, signal, token) in projected:
                    counts[(signal, token)] += 1
    return {
        signal: sorted(token for (kind, token), count in counts.items() if kind == signal and count >= 2)
        for signal in SIGNALS
    }


def signal_coverage(
    members: Sequence[AccountAggregate], shared: Mapping[str, Sequence[str]]
) -> dict[str, float]:
    """신호별로 공유 토큰에 연결된 계정의 비율을 계산합니다."""

    return {
        signal: sum(1 for member in members if member.tokens[signal] & set(shared[signal])) / len(members)
        for signal in SIGNALS
    }


def score_v2(
    account_count: int, coverage: Mapping[str, float], parameters: ScoreParameters = DEFAULT_PARAMETERS
) -> float:
    """관계 강도와 구별 계정 수로 0~1의 조사 우선순위를 계산합니다.

    relation = Σ(가중치 × 커버리지) / 가중치 합계
    size = min((계정 수 - 1) / (포화 계정 수 - 1), 1)
    score = relation × (size_base + (1 - size_base) × size)
    공유 신호가 한 종류뿐이면 대리결제나 공용망 가능성을 반영해 감점합니다.
    티켓 수는 입력받지 않으므로 한 계정의 매수는 점수를 올리지 못합니다.
    """

    if account_count < 2:
        raise ValueError("score-v2 requires at least two distinct accounts")
    relation = sum(parameters.signal_weights[s] * coverage[s] for s in SIGNALS) / parameters.weight_sum
    size = min((account_count - 1) / (parameters.size_saturation_accounts - 1), 1.0)
    score = relation * (parameters.size_base + (1 - parameters.size_base) * size)
    if sum(1 for s in SIGNALS if coverage[s] > 0) == 1:
        score *= parameters.single_signal_type_factor
    return round(min(score, 1.0), 4)


def priority_band(score: float, parameters: ScoreParameters = DEFAULT_PARAMETERS) -> str:
    """평가 보고용 실험 등급입니다. 운영 응답이나 제재 판단에 쓰지 않습니다."""

    if score >= parameters.high_band:
        return "HIGH"
    return "MEDIUM" if score >= parameters.medium_band else "LOW"


def _coerce_index(value: object) -> int:
    if type(value) is float:
        if not value.is_integer():
            raise GraphResultInvalid("Invalid Louvain member mapping")
        return int(value)
    if type(value) is not int:
        raise GraphResultInvalid("Invalid Louvain result")
    return value


def clusters_from_account_communities(
    request: Track1AnalysisRequest,
    graph: AccountTokenGraph,
    records: Sequence[Mapping[str, object]],
    parameters: ScoreParameters = DEFAULT_PARAMETERS,
) -> list[Track1Cluster]:
    """GDS의 계정 커뮤니티를 요청에 실제 있는 예매 단위 members로 펼칩니다."""

    communities: dict[int, list[int]] = defaultdict(list)
    seen: set[int] = set()
    for record in records:
        account_index = _coerce_index(record.get("accountIndex"))
        community_id = record.get("communityId")
        if type(community_id) is not int:
            raise GraphResultInvalid("Invalid Louvain result")
        if account_index == -1:
            continue
        if not 0 <= account_index < len(graph.accounts) or account_index in seen:
            raise GraphResultInvalid("Invalid Louvain member mapping")
        seen.add(account_index)
        communities[community_id].append(account_index)
    if len(seen) != len(graph.accounts):
        raise GraphResultInvalid("Incomplete Louvain result")

    clusters = []
    for indexes in communities.values():
        members = [graph.accounts[index] for index in indexes]
        if len(members) < 2:
            continue
        if len({member.key[0] for member in members}) != 1:
            raise GraphResultInvalid("Louvain community crosses event scope")
        shared = community_shared_tokens(members, graph)
        if not any(shared.values()):
            continue
        tickets = sorted((t for member in members for t in member.tickets), key=lambda t: t.key)
        member_keys = [t.key for t in tickets]
        identity = json.dumps(
            [str(request.request_id), member_keys], ensure_ascii=False, separators=(",", ":")
        )
        clusters.append(Track1Cluster(
            clusterId="cluster-" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            members=[Track1ClusterMember(
                accountId=t.account_id, reservationNo=t.reservation_no, eventId=t.event_id,
            ) for t in tickets],
            paymentHashes=shared["payment_hash"],
            addressHashes=shared["address_hash"],
            deviceIdHashes=shared["device_id_hash"],
            ipHashes=shared["ip_hash"],
            riskScore=score_v2(len(members), signal_coverage(members, shared), parameters),
        ))
    return sorted(clusters, key=lambda cluster: cluster.cluster_id)


# 수량 규칙은 다계정 군집과 별개의 조사 신호입니다.
QUANTITY_EVALUATED = "EVALUATED"
QUANTITY_NOT_EVALUATED = "NOT_EVALUATED"
REASON_POLICY_MISSING = "POLICY_MISSING"
REASON_QUANTITY_INCOMPLETE = "QUANTITY_INCOMPLETE"


@dataclass(frozen=True)
class PurchaseLimitPolicy:
    """Spring이 검증해 전달할 공연 범위의 계정별 한도를 나타냅니다."""

    max_tickets_per_account: int
    policy_version: str

    def __post_init__(self) -> None:
        if type(self.max_tickets_per_account) is not int or self.max_tickets_per_account < 1:
            raise ValueError("max_tickets_per_account must be a positive integer")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be blank")


@dataclass(frozen=True)
class QuantityFinding:
    account_key: AccountKey
    valid_quantity: int
    max_tickets_per_account: int
    policy_version: str


@dataclass(frozen=True)
class EventQuantityResult:
    event_id: str
    status: str
    reason: str | None = None
    policy_version: str | None = None
    findings: tuple[QuantityFinding, ...] = ()


def unit_row_quantities(tickets: Iterable[Track1Ticket]) -> dict[AccountKey, int]:
    """1행이 유효 티켓 1매임이 보장된 합성 입력에서만 쓰는 수량 집계입니다."""

    return {key: account.ticket_count for key, account in aggregate_accounts(tickets).items()}


def purchase_limit_evaluations(request: Track1LimitAnalysisRequest) -> list[Track1PurchaseLimitEvaluation]:
    """제출된 회차 범위마다 v2 계약의 한도 평가 결과를 하나씩 만듭니다.

    정책 미확인, 수량 불완전, 정책 기간 불일치는 내부 NOT_EVALUATED에 해당하며
    계약 상태로 구분해 돌려줍니다. 어느 경우에도 기본 한도를 적용하지 않습니다.
    """

    tickets_by_scope: dict[str, list] = defaultdict(list)
    for ticket in request.tickets:
        tickets_by_scope[ticket.analysis_scope].append(ticket)

    results = []
    for scope in request.performance_scopes:
        scope_key = scope.performance_id.lower()
        tickets = tickets_by_scope[scope_key]
        policy = scope.policy
        identity = {
            "internalEventId": scope.internal_event_id,
            "eventId": scope.event_id,
            "performanceId": scope.performance_id,
        }

        def not_evaluated(status: str) -> Track1PurchaseLimitEvaluation:
            return Track1PurchaseLimitEvaluation(
                **identity, status=status,
                policyVersion=policy.policy_version if policy else None, accountLimitFindings=[],
            )

        if policy is None:
            results.append(not_evaluated("POLICY_NOT_CONFIRMED"))
            continue
        # 스냅샷 기준 시각 이후의 구매는 수량에 반영됐다고 볼 수 없어 한도를 판정하지 않습니다.
        if not scope.quantity_is_complete or any(ticket.purchased_at > scope.quantity_as_of for ticket in tickets):
            results.append(not_evaluated("QUANTITY_DATA_INCOMPLETE"))
            continue
        if not all(policy.applies_at(ticket.purchased_at) for ticket in tickets):
            results.append(not_evaluated("POLICY_TIME_NOT_APPLICABLE"))
            continue

        by_account: dict[str, list] = defaultdict(list)
        for ticket in tickets:
            by_account[ticket.account_id].append(ticket)
        quantities = {
            (scope_key, account): sum(t.effective_ticket_quantity for t in owned)
            for account, owned in by_account.items()
        }
        internal = PurchaseLimitPolicy(policy.max_tickets_per_account, policy.policy_version)
        evaluated = evaluate_account_quantities(quantities, {scope_key: internal}, [scope_key])
        findings = evaluated[0].findings if evaluated else ()
        results.append(Track1PurchaseLimitEvaluation(
            **identity, status="EVALUATED", policyVersion=policy.policy_version,
            accountLimitFindings=[
                Track1AccountLimitFinding(
                    accountId=finding.account_key[1],
                    effectiveTicketQuantity=finding.valid_quantity,
                    maxTicketsPerAccount=finding.max_tickets_per_account,
                    policyVersion=finding.policy_version,
                    reservationQuantities=[
                        Track1ReservationQuantity(
                            reservationNo=t.reservation_no, effectiveTicketQuantity=t.effective_ticket_quantity,
                        )
                        for t in sorted(by_account[finding.account_key[1]], key=lambda t: t.reservation_no)
                    ],
                )
                for finding in findings
            ],
        ))
    return results


def evaluate_account_quantities(
    quantities: Mapping[AccountKey, int],
    policies: Mapping[str, PurchaseLimitPolicy],
    complete_events: Iterable[str],
) -> list[EventQuantityResult]:
    """공연별로 계정 한도 초과만 판정합니다.

    정책이나 수량 완전성이 없으면 기본 한도를 쓰지 않고 NOT_EVALUATED로 둡니다.
    계정들의 합계는 계산하지 않습니다. 동일인 합산 규칙은 승인된 정책이 없어 구현하지 않습니다.
    """

    by_event: dict[str, dict[AccountKey, int]] = defaultdict(dict)
    for key, quantity in quantities.items():
        if type(quantity) is not int or quantity < 0:
            raise ValueError("valid quantity must be a non-negative integer")
        by_event[key[0]][key] = quantity

    complete = set(complete_events)
    results = []
    for event_id in sorted(by_event):
        policy = policies.get(event_id)
        if policy is None:
            results.append(EventQuantityResult(event_id, QUANTITY_NOT_EVALUATED, REASON_POLICY_MISSING))
            continue
        if event_id not in complete:
            results.append(EventQuantityResult(
                event_id, QUANTITY_NOT_EVALUATED, REASON_QUANTITY_INCOMPLETE,
                policy_version=policy.policy_version,
            ))
            continue
        findings = tuple(
            QuantityFinding(key, quantity, policy.max_tickets_per_account, policy.policy_version)
            for key, quantity in sorted(by_event[event_id].items())
            if quantity > policy.max_tickets_per_account
        )
        results.append(EventQuantityResult(
            event_id, QUANTITY_EVALUATED, policy_version=policy.policy_version, findings=findings,
        ))
    return results
