"""다회차 조사 후보의 연결 조건과 점수 구성을 정의하는 순수 함수 모음입니다.

JSON 경로(`services.multi_event_analysis`)와 대용량 SQLite 경로
(`services.multi_event_artifact_jobs`)가 같은 규칙을 쓰도록 이 모듈만 판정과 점수 계산을
담당합니다. 입출력은 계정 가명 참조와 집계 값뿐이며 신호 token 원문을 반환하지 않습니다.

연결 조건(후보를 만드는 근거)과 점수 구성(후보의 L 값)은 서로 다릅니다.

- 연결 조건: (a) 강한 신호(refund, phone, device, address)의 같은 token을 서로 다른 두 회차에서
  같은 GDS 군집 안의 계정들이 공유한 경우의 정확한 교집합, (b) 같은 계정 집합이 서로 다른 두
  회차의 GDS Louvain 결과에서 각각 같은 군집에 있었던 경우의 정확한 교집합
  (`stable_account_cocluster`). 추이 병합은 하지 않습니다. 결제와 IP 시간창은 단독 연결
  조건이 아닙니다.
- 점수 구성: 후보 구성원 두 명 이상이 출처 회차에서 실제로 공유한 모든 exp2 신호의 coverage와
  각 회차 `damping()` 결과의 평균으로 L을 계산합니다.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Hashable, Iterable, Mapping, Sequence

from services.track1_scoring_exp2 import (
    BASE_SIGNALS,
    DEFAULT_EXP2_PARAMETERS,
    STRONG_RECURRENCE_SIGNALS,
    Exp2Parameters,
    Exp2Score,
    QuantityContext,
    damping,
    priority_band_exp2,
    score_exp2,
)


SCORE_VERSION_MULTI_PERFORMANCE = "score-v2-exp2-multi-performance-recurrence"
STABLE_ACCOUNT_COCLUSTER = "stable_account_cocluster"
RELATIONSHIP_TYPES = frozenset(STRONG_RECURRENCE_SIGNALS | {STABLE_ACCOUNT_COCLUSTER})
IP_WINDOW_PREFIX = "ip_window:"
# unavailableSignals에서 망 유형별 IP 시간창 신호를 하나로 묶어 표시하는 이름입니다.
IP_WINDOW_FAMILY = "ip_window"
NORMAL_SHARED_POSSIBLE = "NORMAL_SHARED_POSSIBLE"
MAX_RECURRENCE_COMPARISONS = 2_000_000


class RecurrenceComparisonLimit(Exception):
    """반복 비교 상한을 넘었을 때 결과를 자르지 않고 실패시키기 위한 예외입니다."""

    code = "RECURRENCE_COMPARISON_LIMIT_EXCEEDED"

    def __init__(self) -> None:
        super().__init__(self.code)


def recurrence_pair_allowed(left_performance_id: str, right_performance_id: str) -> bool:
    """반복 근거는 서로 다른 회차 쌍에서만 인정합니다. 같은 공연의 다른 회차도 인정합니다."""

    return left_performance_id != right_performance_id


def cocluster_comparison_count(membership_counts: Iterable[int]) -> int:
    """계정별 (회차, 군집) 소속 수로 역색인 쌍 비교 횟수의 상한을 계산합니다."""

    total = 0
    for count in membership_counts:
        if type(count) is not int or count < 0:
            raise ValueError("membership count must be a non-negative integer")
        total += count * (count - 1) // 2
    return total


def require_comparison_budget(comparisons: int, limit: int = MAX_RECURRENCE_COMPARISONS) -> None:
    if comparisons > limit:
        raise RecurrenceComparisonLimit()


def cocluster_pairs(
    memberships: Sequence[tuple[Hashable, str, Hashable]],
) -> list[tuple[tuple[Hashable, Hashable], tuple[Hashable, Hashable]]]:
    """한 계정의 (scope, performance, community) 소속 목록에서 서로 다른 회차 쌍을 만듭니다.

    반환 원소는 정렬된 ((left_scope, left_community), (right_scope, right_community))입니다.
    같은 키를 가진 계정들의 집합이 곧 두 회차에서 같은 군집에 있었던 정확한 교집합입니다.
    """

    ordered = sorted(memberships, key=lambda item: (str(item[0]), str(item[2])))
    pairs = []
    for left_index, (left_scope, left_performance, left_community) in enumerate(ordered):
        for right_scope, right_performance, right_community in ordered[left_index + 1:]:
            if not recurrence_pair_allowed(left_performance, right_performance):
                continue
            pairs.append(((left_scope, left_community), (right_scope, right_community)))
    return pairs


def is_ip_window(signal: str) -> bool:
    return signal.startswith(IP_WINDOW_PREFIX)


@dataclass(frozen=True)
class LinkInputs:
    coverage: Mapping[str, float]
    mean_damping: Mapping[str, float]
    excluded_signals: tuple[str, ...]


def token_damping(
    signal: str, owner_count: int, scope_account_count: int,
    params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS,
) -> float | None:
    """각 회차의 exp2 `damping()` 결과를 그대로 사용합니다. None이면 점수에서 제외합니다."""

    return damping(signal, owner_count, max(1, scope_account_count), params)


def link_inputs(
    member_count: int,
    covered_by_signal: Mapping[str, int],
    damping_by_signal: Mapping[str, Sequence[float]],
    excluded_signals: Iterable[str] = (),
) -> LinkInputs:
    """후보 구성원이 공유한 신호별 coverage와 평균 감쇠를 계산합니다.

    `covered_by_signal`은 제외되지 않은 공유 token에 연결된 서로 다른 구성원 수,
    `damping_by_signal`은 (회차, token)마다의 감쇠 값 목록입니다. 부동소수 합의 순서
    의존을 없애려고 정렬 후 `math.fsum`으로 평균을 냅니다.
    """

    if type(member_count) is not int or member_count < 2:
        raise ValueError("candidate requires at least two accounts")
    coverage: dict[str, float] = {}
    mean_damping: dict[str, float] = {}
    for signal in sorted(covered_by_signal):
        values = sorted(damping_by_signal.get(signal, ()))
        covered = covered_by_signal[signal]
        if not values or covered < 2:
            continue
        coverage[signal] = min(covered / member_count, 1.0)
        mean_damping[signal] = math.fsum(values) / len(values)
    return LinkInputs(coverage, mean_damping, tuple(sorted(set(excluded_signals))))


@dataclass(frozen=True)
class CandidateEvaluation:
    score: Exp2Score
    priority_band: str
    distinct_event_count: int
    distinct_performance_count: int
    available_signals: tuple[str, ...]
    excluded_signals: tuple[str, ...]
    unavailable_signals: tuple[str, ...]
    normal_sharing_possibilities: tuple[str, ...]


def candidate_qualifies(member_count: int, performance_ids: Iterable[str], link: LinkInputs) -> bool:
    """후보 조건: 계정 2개 이상, 서로 다른 회차 2개 이상, 구성원이 직접 공유한 exp2 신호 1개 이상."""

    return member_count >= 2 and len(set(performance_ids)) >= 2 and bool(link.coverage)


def evaluate_candidate(
    member_count: int,
    source_scopes: Sequence[tuple[str, str]],
    link: LinkInputs,
    quantity: QuantityContext,
    params: Exp2Parameters = DEFAULT_EXP2_PARAMETERS,
) -> CandidateEvaluation | None:
    """후보 하나의 점수를 계산합니다. `source_scopes`는 (event_id, performance_id) 목록입니다.

    R은 사전 등록 정의대로 서로 다른 회차 수로 계산합니다.
    """

    event_ids = {event_id for event_id, _ in source_scopes}
    performance_ids = {performance_id for _, performance_id in source_scopes}
    if not candidate_qualifies(member_count, performance_ids, link):
        return None
    score = score_exp2(
        member_count, link.coverage, link.mean_damping, quantity, len(performance_ids),
        params=params, excluded_signals=link.excluded_signals,
    )
    available = tuple(sorted(link.coverage))
    unavailable = [signal for signal in sorted(BASE_SIGNALS) if signal not in link.coverage]
    if not any(is_ip_window(signal) for signal in link.coverage):
        unavailable.append(IP_WINDOW_FAMILY)
    # 공유 신호는 가족, 사무실, 기숙사처럼 정상적인 공동 사용일 수 있으므로 항상 함께 표시합니다.
    normal = (NORMAL_SHARED_POSSIBLE,) if available else ()
    return CandidateEvaluation(
        score=score,
        priority_band=priority_band_exp2(score.score, params),
        distinct_event_count=len(event_ids),
        distinct_performance_count=len(performance_ids),
        available_signals=available,
        excluded_signals=tuple(score.excluded_signals),
        unavailable_signals=tuple(sorted(unavailable)),
        normal_sharing_possibilities=normal,
    )
