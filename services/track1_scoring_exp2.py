"""score-v2-exp2의 고정 파라미터와 설명 가능한 관계 계산입니다.

이 모듈은 score-v2-exp1을 대체하지 않습니다. exp2는 별도 실험 버전이며,
점수는 조사 우선순위일 뿐 불법 행위 확률이나 동일인 확정 결과가 아닙니다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import math
from typing import Iterable, Mapping, Protocol, Sequence


SCORE_VERSION_EXP2 = "score-v2-exp2"
PROJECTION_VERSION_EXP2 = "projection-v2.2-exp2-ipwindow-softhub"

BASE_SIGNALS = (
    "refund_account_hash",
    "phone_hash",
    "device_id_hash",
    "address_hash",
    "payment_hash",
)
IP_NETWORK_TYPES = frozenset({
    "MOBILE_CGNAT", "RESIDENTIAL", "CORPORATE_EDU", "DATACENTER_PROXY", "UNKNOWN",
})
STRONG_RECURRENCE_SIGNALS = frozenset({
    "refund_account_hash", "phone_hash", "device_id_hash", "address_hash",
})


class Exp2Ticket(Protocol):
    account_id: str
    reservation_no: str
    payment_hash: str | None
    address_hash: str | None
    device_id_hash: str | None
    ip_hash: str | None
    refund_account_hash: str | None
    phone_hash: str | None

    @property
    def analysis_scope(self) -> str: ...


@dataclass(frozen=True)
class Exp2Parameters:
    """사전 등록된 exp2 실험 파라미터입니다."""

    reliability: Mapping[str, float] = field(default_factory=lambda: {
        "refund_account_hash": 0.80,
        "phone_hash": 0.70,
        "device_id_hash": 0.60,
        "address_hash": 0.60,
        "payment_hash": 0.20,
        "ip_window:RESIDENTIAL": 0.50,
        "ip_window:CORPORATE_EDU": 0.50,
        "ip_window:DATACENTER_PROXY": 0.60,
        "ip_window:UNKNOWN": 0.30,
        "ip_window:MOBILE_CGNAT": 0.10,
    })
    soft_hub_k: Mapping[str, int] = field(default_factory=lambda: {
        "refund_account_hash": 10,
        "phone_hash": 5,
        "device_id_hash": 5,
        "address_hash": 10,
        "payment_hash": 3,
        "ip_window:RESIDENTIAL": 30,
        "ip_window:CORPORATE_EDU": 30,
        "ip_window:DATACENTER_PROXY": 30,
        "ip_window:UNKNOWN": 10,
        "ip_window:MOBILE_CGNAT": 3,
    })
    hard_hub_h: Mapping[str, int | None] = field(default_factory=lambda: {
        "refund_account_hash": None,
        "phone_hash": None,
        "device_id_hash": 30,
        "address_hash": 50,
        "payment_hash": 20,
        "ip_window:RESIDENTIAL": 100,
        "ip_window:CORPORATE_EDU": 100,
        "ip_window:DATACENTER_PROXY": 100,
        "ip_window:UNKNOWN": 30,
        "ip_window:MOBILE_CGNAT": 9,
    })
    hub_relative_percent: int = 1
    ip_window_seconds: int = 600
    size_base: float = 0.6
    size_saturation_log2: float = 5.0
    quantity_neutral: float = 0.5
    recurrence_saturation: int = 4
    high_band: float = 0.40
    medium_band: float = 0.20


DEFAULT_EXP2_PARAMETERS = Exp2Parameters()


@dataclass(frozen=True)
class Exp2Account:
    scope: str
    account_id: str
    reservation_nos: tuple[str, ...]
    tokens: Mapping[str, frozenset[str]]

    @property
    def key(self) -> tuple[str, str]:
        return self.scope, self.account_id


@dataclass(frozen=True)
class Exp2Graph:
    accounts: tuple[Exp2Account, ...]
    tokens: tuple[tuple[str, str, str], ...]
    edges: tuple[dict[str, object], ...]
    token_damping: Mapping[tuple[str, str, str], float]
    excluded_tokens: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class QuantityContext:
    value: float
    status: str
    reason: str | None


@dataclass(frozen=True)
class Exp2Score:
    score: float
    link: float
    size: float
    behavior: float
    quantity: float
    recurrence: float
    available_signals: tuple[str, ...]
    excluded_signals: tuple[str, ...]
    coverage: Mapping[str, float]
    quantity_status: str
    quantity_reason: str | None


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _network_type(value: object) -> str:
    return value if isinstance(value, str) and value in IP_NETWORK_TYPES else "UNKNOWN"


def ip_window_token(ticket: Exp2Ticket, params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS) -> str | None:
    """UTC 구매 시각이 검증된 경우에만 기존 가명 IP로 시간창 토큰을 만듭니다."""

    purchased_at = getattr(ticket, "purchased_at", None)
    ip_hash = getattr(ticket, "ip_hash", None)
    if not isinstance(purchased_at, datetime) or purchased_at.tzinfo is None or purchased_at.utcoffset() is None or not ip_hash:
        return None
    if params.ip_window_seconds < 1:
        raise ValueError("ip_window_seconds must be positive")
    bucket = int(purchased_at.timestamp()) // params.ip_window_seconds
    raw = f"ipw|{ticket.analysis_scope}|{ip_hash}|{bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def damping(
    signal_key: str,
    distinct_accounts: int,
    scope_accounts: int,
    params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS,
) -> float | None:
    """사전 등록된 soft hub 감쇠와 hard hub 제외를 적용합니다."""

    if signal_key not in params.reliability:
        raise ValueError("unknown exp2 signal")
    if type(distinct_accounts) is not int or distinct_accounts < 1:
        raise ValueError("distinct_accounts must be a positive integer")
    if type(scope_accounts) is not int or scope_accounts < 1:
        raise ValueError("scope_accounts must be a positive integer")
    if type(params.hub_relative_percent) is not int or params.hub_relative_percent < 0:
        raise ValueError("hub_relative_percent must be a non-negative integer")
    k = params.soft_hub_k[signal_key]
    if type(k) is not int or k < 1:
        raise ValueError("soft hub threshold must be positive")
    h = params.hard_hub_h[signal_key]
    relative = -(-scope_accounts * params.hub_relative_percent // 100)
    if h is not None and (type(h) is not int or h < 1):
        raise ValueError("hard hub threshold must be positive or None")
    if h is not None and distinct_accounts >= max(h, relative):
        return None
    return 1.0 if distinct_accounts <= k else math.sqrt(k / distinct_accounts)


def _aggregate_accounts(tickets: Iterable[Exp2Ticket], params: Exp2Parameters) -> dict[tuple[str, str], Exp2Account]:
    grouped: dict[tuple[str, str], list[Exp2Ticket]] = defaultdict(list)
    for ticket in tickets:
        grouped[(ticket.analysis_scope, ticket.account_id)].append(ticket)
    result: dict[tuple[str, str], Exp2Account] = {}
    for key, rows in grouped.items():
        tokens: dict[str, set[str]] = {signal: set() for signal in BASE_SIGNALS}
        for row in rows:
            for signal in BASE_SIGNALS:
                token = getattr(row, signal, None)
                if isinstance(token, str) and token:
                    tokens[signal].add(token)
        result[key] = Exp2Account(
            scope=key[0], account_id=key[1],
            reservation_nos=tuple(sorted({row.reservation_no for row in rows})),
            tokens={signal: frozenset(values) for signal, values in tokens.items()},
        )
    return result


def build_account_token_graph_exp2(
    tickets: Sequence[Exp2Ticket], params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS,
) -> Exp2Graph:
    """exp2 전용 가중 투영을 결정적으로 만듭니다.

    고정 IP 토큰은 투영하지 않습니다. IP 시간창은 회차 범위와 UTC 600초 bucket을
    포함하고, 하나의 창에 복수 망 유형이 있으면 UNKNOWN 신호로 처리합니다.
    """

    accounts = _aggregate_accounts(tickets, params)
    account_tokens: dict[tuple[str, str], dict[str, set[str]]] = {
        key: {signal: set(values) for signal, values in account.tokens.items()}
        for key, account in accounts.items()
    }
    ip_types: dict[tuple[str, str], set[str]] = defaultdict(set)
    ip_owners: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for ticket in tickets:
        token = ip_window_token(ticket, params)
        if token is not None:
            ip_types[(ticket.analysis_scope, token)].add(_network_type(getattr(ticket, "ip_network_type", None)))
            ip_owners[(ticket.analysis_scope, token)].add((ticket.analysis_scope, ticket.account_id))
    for (scope, token), types in ip_types.items():
        kind = next(iter(types)) if len(types) == 1 else "UNKNOWN"
        signal = f"ip_window:{kind}"
        for account_key in ip_owners[(scope, token)]:
            account_tokens[account_key].setdefault(signal, set()).add(token)

    owners: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    for account_key, by_signal in account_tokens.items():
        for signal, values in by_signal.items():
            for token in values:
                owners[(account_key[0], signal, token)].add(account_key)
    scope_counts = Counter(scope for scope, _ in accounts)
    projected: list[tuple[str, str, str]] = []
    damping_values: dict[tuple[str, str, str], float] = {}
    excluded: list[tuple[str, str, str]] = []
    for key in sorted(owners):
        if len(owners[key]) < 2:
            continue
        value = damping(key[1], len(owners[key]), scope_counts[key[0]], params)
        if value is None:
            excluded.append(key)
        else:
            projected.append(key)
            damping_values[key] = value
    connected = sorted({account for token in projected for account in owners[token]})
    account_index = {account: index for index, account in enumerate(connected)}
    token_index = {token: len(connected) + index for index, token in enumerate(projected)}
    edges = tuple(
        {
            "source": account_index[account],
            "target": token_index[token],
            "account_index": account_index[account],
            "weight": params.reliability[token[1]] * damping_values[token],
        }
        for token in projected for account in sorted(owners[token])
    )
    materialized = []
    for key in connected:
        by_signal = account_tokens[key]
        materialized.append(Exp2Account(
            scope=key[0], account_id=key[1], reservation_nos=accounts[key].reservation_nos,
            tokens={signal: frozenset(values) for signal, values in by_signal.items()},
        ))
    return Exp2Graph(
        accounts=tuple(materialized), tokens=tuple(projected), edges=edges,
        token_damping=damping_values, excluded_tokens=tuple(excluded),
    )


def quantity_context(value: float | None, reason: str | None = None, params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS) -> QuantityContext:
    """수량 정책 또는 완전성이 없을 때 중립값과 사유를 분리해 보존합니다."""

    if value is None:
        return QuantityContext(params.quantity_neutral, "NEUTRAL_NO_POLICY", reason or "POLICY_OR_QUANTITY_UNAVAILABLE")
    parsed = _finite(value, "quantity")
    if not 0.0 <= parsed <= 1.0:
        raise ValueError("quantity must be between zero and one")
    return QuantityContext(parsed, "VERIFIED", reason)


def score_exp2(
    member_count: int,
    coverage: Mapping[str, float],
    mean_damping: Mapping[str, float],
    quantity: QuantityContext | None = None,
    p_count: int = 1,
    params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS,
    excluded_signals: Iterable[str] = (),
) -> Exp2Score:
    """noisy-OR, 규모, 수량과 회차 반복 근거를 분리해 계산합니다."""

    if type(member_count) is not int or member_count < 2:
        raise ValueError("score-v2-exp2 requires at least two distinct accounts")
    if type(p_count) is not int or p_count < 1:
        raise ValueError("p_count must be a positive integer")
    if (type(params.recurrence_saturation) is not int or params.recurrence_saturation < 2
            or not math.isfinite(params.size_saturation_log2) or params.size_saturation_log2 <= 0
            or not math.isfinite(params.size_base) or not 0.0 <= params.size_base <= 1.0
            or not math.isfinite(params.quantity_neutral) or not 0.0 <= params.quantity_neutral <= 1.0):
        raise ValueError("invalid exp2 parameters")
    for signal, reliability in params.reliability.items():
        if not math.isfinite(reliability) or not 0.0 <= reliability <= 1.0:
            raise ValueError(f"invalid reliability for {signal}")
    components: list[tuple[str, float]] = []
    for signal in sorted(coverage):
        if signal not in params.reliability or signal not in mean_damping:
            raise ValueError("coverage signal is not an exp2 signal")
        c = _finite(coverage[signal], "coverage")
        d = _finite(mean_damping[signal], "damping")
        if not 0.0 <= c <= 1.0 or not 0.0 <= d <= 1.0:
            raise ValueError("coverage and damping must be between zero and one")
        component = params.reliability[signal] * d * c
        if not 0.0 <= component <= 1.0:
            raise ValueError("invalid noisy-or component")
        if component > 0:
            components.append((signal, component))
    product = 1.0
    for _, component in components:
        product *= 1.0 - component
    link = 1.0 - product
    size = params.size_base + (1.0 - params.size_base) * min(math.log2(member_count) / params.size_saturation_log2, 1.0)
    context = quantity or quantity_context(None, params=params)
    q = _finite(context.value, "quantity")
    if not 0.0 <= q <= 1.0:
        raise ValueError("quantity must be between zero and one")
    recurrence = min((p_count - 1) / (params.recurrence_saturation - 1), 1.0)
    behavior = 0.5 * q + 0.5 * recurrence
    score = round(link * size * behavior, 4)
    return Exp2Score(
        score=score, link=link, size=size, behavior=behavior, quantity=q, recurrence=recurrence,
        available_signals=tuple(signal for signal, _ in components),
        excluded_signals=tuple(sorted(set(excluded_signals))),
        coverage={signal: coverage[signal] for signal, _ in components},
        quantity_status=context.status, quantity_reason=context.reason,
    )


def priority_band_exp2(score: float, params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS) -> str:
    value = _finite(score, "score")
    if not 0.0 <= value <= 1.0:
        raise ValueError("score must be between zero and one")
    if value >= params.high_band:
        return "HIGH"
    return "MEDIUM" if value >= params.medium_band else "LOW"


@dataclass(frozen=True)
class RecurrenceCluster:
    """회차 간 반복 근거 계산만을 위한 군집 요약입니다."""

    cluster_id: str
    scope: str
    account_ids: frozenset[str]
    tokens: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class RecurrenceComponent:
    """A6 research-only union-find group across per-performance clusters."""

    cluster_ids: tuple[str, ...]
    scopes: frozenset[str]
    account_ids: frozenset[str]


def recurrence_components(clusters: Sequence[RecurrenceCluster]) -> tuple[RecurrenceComponent, ...]:
    """Union clusters only by stable accounts or verified strong signal tokens.

    This is the A6 offline recurrence model. It may join overlapping groups by
    transitive closure and is deliberately separate from product candidates.
    """
    ordered = sorted(clusters, key=lambda item: item.cluster_id)
    if len({cluster.cluster_id for cluster in ordered}) != len(ordered):
        raise ValueError("recurrence cluster IDs must be unique")

    parent = list(range(len(clusters)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    seen_accounts: dict[str, int] = {}
    seen_tokens: dict[tuple[str, str], int] = {}
    for index, cluster in enumerate(ordered):
        for account_id in sorted(cluster.account_ids):
            if account_id in seen_accounts:
                union(index, seen_accounts[account_id])
            else:
                seen_accounts[account_id] = index
        for signal in STRONG_RECURRENCE_SIGNALS:
            for token in sorted(cluster.tokens.get(signal, frozenset())):
                key = signal, token
                if key in seen_tokens:
                    union(index, seen_tokens[key])
                else:
                    seen_tokens[key] = index
    grouped: dict[int, list[RecurrenceCluster]] = defaultdict(list)
    for index, cluster in enumerate(ordered):
        grouped[root(index)].append(cluster)
    components = [
        RecurrenceComponent(
            cluster_ids=tuple(sorted(cluster.cluster_id for cluster in members)),
            scopes=frozenset(cluster.scope for cluster in members),
            account_ids=frozenset(account for cluster in members for account in cluster.account_ids),
        )
        for members in grouped.values()
    ]
    return tuple(sorted(components, key=lambda item: item.cluster_ids))


def recurrence_scope_counts(clusters: Sequence[RecurrenceCluster]) -> Mapping[str, int]:
    """Return distinct performance counts from the A6 research-only components."""
    counts = {}
    for component in recurrence_components(clusters):
        for cluster_id in component.cluster_ids:
            counts[cluster_id] = len(component.scopes)
    return counts
