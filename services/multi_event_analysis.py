"""고정 원장 범위를 위한 다회차 조사 후보 분석입니다.

회차별 exp1 및 exp2 경로를 변경하지 않습니다. 이 모듈은 고정된 여러 원장에서
검증된 안정 계정 참조와 GDS Louvain 결과만으로 조사 후보를 제시합니다. 연결 조건과
점수 구성은 `services.multi_event_candidates`의 순수 함수가 정의하며, 대용량 SQLite 경로도
같은 함수를 사용합니다.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from typing import Iterable

from schemas.multi_event import (
    MultiEventAnalysisRequest,
    MultiEventAnalysisResponse,
    MultiEventCandidate,
    MultiEventLedgerScope,
    MultiEventRelationship,
    MultiEventScoreBreakdown,
    MultiEventTicketInput,
)
from services.multi_event_candidates import (
    STABLE_ACCOUNT_COCLUSTER,
    cocluster_comparison_count,
    cocluster_pairs,
    evaluate_candidate,
    link_inputs,
    recurrence_pair_allowed,
    require_comparison_budget,
    token_damping,
)
from services.track1_scoring_exp2 import (
    BASE_SIGNALS,
    SCORE_VERSION_EXP2,
    STRONG_RECURRENCE_SIGNALS,
    ip_window_token,
    quantity_context,
)


ScopeKey = tuple[str, str, str]
TokenKey = tuple[str, str]


def _candidate_id(analysis_id: str, accounts: Iterable[str]) -> str:
    raw = json.dumps([analysis_id, sorted(accounts)], separators=(",", ":"))
    return "multi-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scope_rows(request: MultiEventAnalysisRequest, keys: set[ScopeKey]) -> list[MultiEventLedgerScope]:
    return sorted((scope for scope in request.ledger_scopes if scope.key in keys), key=lambda scope: scope.key)


def _scope_token_owners(tickets: list[MultiEventTicketInput]) -> dict[TokenKey, set[str]]:
    """한 회차의 exp2 신호 token별 소유 계정을 만듭니다.

    SQLite 경로의 적재 규칙과 같습니다. 고정값 IP는 점수에 쓰지 않고, IP 시간창은 망 유형이
    하나면 그 유형, 섞였거나 없으면 UNKNOWN으로 판정합니다.
    """

    owners: dict[TokenKey, set[str]] = defaultdict(set)
    ip_types: dict[str, set[str]] = defaultdict(set)
    ip_owners: dict[str, set[str]] = defaultdict(set)
    for ticket in tickets:
        for signal in BASE_SIGNALS:
            token = getattr(ticket, signal)
            if token is not None:
                owners[(signal, token)].add(ticket.account_id)
        window = ip_window_token(ticket)
        if window is not None:
            ip_types[window].add(ticket.ip_network_type or "UNKNOWN")
            ip_owners[window].add(ticket.account_id)
    for window, types in ip_types.items():
        kind = next(iter(types)) if len(types) == 1 else "UNKNOWN"
        owners[(f"ip_window:{kind}", window)] |= ip_owners[window]
    return owners


def _quantity_contexts(request: MultiEventAnalysisRequest):
    """Build one verified quantity context per account and fixed performance scope."""
    provenances: dict[tuple[ScopeKey, str], set[tuple]] = defaultdict(set)
    uncertainties: dict[tuple[ScopeKey, str], set[str]] = defaultdict(set)
    scopes = {scope.key: scope for scope in request.ledger_scopes}
    for ticket in request.tickets:
        key = (ticket.scope_key, ticket.account_id)
        if ticket.quantity_status != "COMPLETE":
            uncertainties[key].add(ticket.quantity_reason or ticket.quantity_status or "QUANTITY_INCOMPLETE")
            continue
        scope = scopes[ticket.scope_key]
        if (scope.policy_snapshot_id is None
                or ticket.quantity_policy_snapshot_id != scope.policy_snapshot_id
                or ticket.quantity_source_version != ticket.source_version
                or ticket.effective_ticket_quantity is None
                or ticket.confirmed_purchase_limit is None
                or ticket.quantity_policy_version is None):
            uncertainties[key].add("QUANTITY_POLICY_SOURCE_MISMATCH")
            continue
        provenances[key].add((ticket.effective_ticket_quantity, ticket.confirmed_purchase_limit,
                              ticket.quantity_policy_snapshot_id, ticket.quantity_policy_version,
                              ticket.quantity_source_version))

    contexts = {}
    for key in provenances.keys() | uncertainties.keys():
        values = provenances.get(key, set())
        reasons = uncertainties.get(key, set())
        if reasons:
            contexts[key] = quantity_context(None, "|".join(sorted(reasons)))
        elif len(values) != 1:
            contexts[key] = quantity_context(None, "QUANTITY_ACCOUNT_OBSERVATION_INCONSISTENT")
        else:
            quantity, limit, *_ = next(iter(values))
            contexts[key] = quantity_context(min(quantity / limit, 1.0), "POLICY_SNAPSHOT_AND_QUANTITY_VERIFIED")
    return contexts


def _quantity_for_candidate(accounts: frozenset[str], source_scopes: list[MultiEventLedgerScope], contexts):
    values: list[float] = []
    uncertainty: set[str] = set()
    for scope in source_scopes:
        for account in accounts:
            context = contexts.get((scope.key, account))
            if context is None:
                uncertainty.add("QUANTITY_SOURCE_SCOPE_MISSING")
                continue
            if context.status != "VERIFIED":
                uncertainty.add(context.reason or context.status)
                continue
            values.append(context.value)
    if uncertainty or not values:
        reason = "|".join(sorted(uncertainty)) if uncertainty else "QUANTITY_SOURCE_UNAVAILABLE"
        return quantity_context(None, reason)
    return quantity_context(sum(values) / len(values), "POLICY_SNAPSHOT_AND_QUANTITY_VERIFIED")


def analyze_multi_event(
    request: MultiEventAnalysisRequest,
    communities_by_scope: dict[ScopeKey, list[frozenset[str]]],
    algorithm_version: str,
) -> MultiEventAnalysisResponse:
    """bounded inverted-index 분석을 수행하고 원본 token을 응답에 넣지 않습니다.

    입력 Pydantic 검증은 공급자 namespace, purpose, key version이 하나로 고정됐음을
    보장한다. 연결 조건은 서로 다른 두 회차에서 (a) 같은 강한 신호 token을 같은 군집 안에서
    공유한 계정의 정확한 교집합, (b) 같은 군집에 함께 있었던 계정의 정확한 교집합뿐이다.
    payment와 IP 시간창은 점수 구성에는 들어가지만 단독 연결 조건이 아니다.
    """

    scopes = {scope.key: scope for scope in request.ledger_scopes}
    tickets_by_scope: dict[ScopeKey, list[MultiEventTicketInput]] = defaultdict(list)
    for ticket in request.tickets:
        tickets_by_scope[ticket.scope_key].append(ticket)
    scope_account_counts = {
        key: len({ticket.account_id for ticket in rows}) for key, rows in tickets_by_scope.items()
    }
    owners_by_scope = {key: _scope_token_owners(rows) for key, rows in tickets_by_scope.items()}
    account_tokens: dict[ScopeKey, dict[str, set[TokenKey]]] = {}
    for key, owners in owners_by_scope.items():
        by_account: dict[str, set[TokenKey]] = defaultdict(set)
        for token_key, accounts in owners.items():
            for account in accounts:
                by_account[account].add(token_key)
        account_tokens[key] = by_account
    quantity_contexts = _quantity_contexts(request)

    # 후보 구성원 집합 -> 관계 유형 -> 근거가 관측된 회차 scope 집합
    relations: dict[frozenset[str], dict[str, set[ScopeKey]]] = defaultdict(lambda: defaultdict(set))

    # (a) 강한 신호 반복: 같은 token이 서로 다른 회차의 같은 군집에서 반복된 정확한 교집합
    evidence_by_token: dict[TokenKey, list[tuple[ScopeKey, frozenset[str]]]] = defaultdict(list)
    for scope_key in sorted(owners_by_scope):
        for (signal, token), owners in sorted(owners_by_scope[scope_key].items()):
            if signal not in STRONG_RECURRENCE_SIGNALS or len(owners) < 2:
                continue
            if token_damping(signal, len(owners), scope_account_counts[scope_key]) is None:
                continue
            # 회차별 전체 GDS Louvain이 같은 군집에 둔 계정만 근거로 남깁니다.
            for community in communities_by_scope.get(scope_key, []):
                community_owners = owners.intersection(community)
                if len(community_owners) >= 2:
                    evidence_by_token[(signal, token)].append((scope_key, frozenset(community_owners)))
    for (signal, _token), observations in sorted(evidence_by_token.items()):
        observations.sort(key=lambda item: (item[0], tuple(sorted(item[1]))))
        for left_index, (left_scope, left_owners) in enumerate(observations):
            for right_scope, right_owners in observations[left_index + 1:]:
                if not recurrence_pair_allowed(scopes[left_scope].performance_id, scopes[right_scope].performance_id):
                    continue
                common = left_owners.intersection(right_owners)
                if len(common) >= 2:
                    relations[frozenset(common)][signal].update((left_scope, right_scope))

    # (b) 같은 계정의 반복 동시 군집: 계정 -> (회차, 군집) 역색인으로 정확한 교집합을 만듭니다.
    memberships: dict[str, list[tuple[ScopeKey, str, int]]] = defaultdict(list)
    for scope_key in sorted(communities_by_scope):
        ordered = sorted(communities_by_scope[scope_key], key=lambda group: tuple(sorted(group)))
        for community_index, community in enumerate(ordered):
            if len(community) < 2:
                continue
            for account in community:
                memberships[account].append((scope_key, scopes[scope_key].performance_id, community_index))
    require_comparison_budget(cocluster_comparison_count(len(items) for items in memberships.values()))
    cocluster_groups: dict[tuple, set[str]] = defaultdict(set)
    for account, items in sorted(memberships.items()):
        for pair in cocluster_pairs(items):
            cocluster_groups[pair].add(account)
    for (left, right), accounts in sorted(cocluster_groups.items()):
        if len(accounts) >= 2:
            relations[frozenset(accounts)][STABLE_ACCOUNT_COCLUSTER].update((left[0], right[0]))

    candidates: list[MultiEventCandidate] = []
    for accounts, by_type in sorted(relations.items(), key=lambda item: tuple(sorted(item[0]))):
        source_keys = set().union(*by_type.values())
        source_scopes = _scope_rows(request, source_keys)

        # 점수 구성: 출처 회차에서 구성원 두 명 이상이 실제로 공유한 모든 exp2 신호
        member_owners: dict[tuple[ScopeKey, str, str], set[str]] = defaultdict(set)
        for scope_key in sorted(source_keys):
            by_account = account_tokens.get(scope_key, {})
            for account in accounts:
                for signal, token in by_account.get(account, ()):
                    member_owners[(scope_key, signal, token)].add(account)
        covered: dict[str, set[str]] = defaultdict(set)
        dampings: dict[str, list[float]] = defaultdict(list)
        excluded: set[str] = set()
        for (scope_key, signal, token), owners in member_owners.items():
            if len(owners) < 2:
                continue
            factor = token_damping(
                signal, len(owners_by_scope[scope_key][(signal, token)]), scope_account_counts[scope_key],
            )
            if factor is None:
                excluded.add(signal)
                continue
            dampings[signal].append(factor)
            covered[signal].update(owners)
        link = link_inputs(len(accounts), {signal: len(values) for signal, values in covered.items()}, dampings, excluded)
        quantity = _quantity_for_candidate(accounts, source_scopes, quantity_contexts)
        evaluation = evaluate_candidate(
            len(accounts), [(scope.event_id, scope.performance_id) for scope in source_scopes], link, quantity,
        )
        if evaluation is None:
            continue
        score = evaluation.score
        relationships = [
            MultiEventRelationship(
                signalType=relation_type,
                accountIds=sorted(accounts),
                sourceScopes=_scope_rows(request, relation_scopes),
            )
            for relation_type, relation_scopes in sorted(by_type.items())
        ]
        candidates.append(MultiEventCandidate(
            candidateId=_candidate_id(request.analysis_id, accounts),
            priorityBand=evaluation.priority_band,
            scoreVersion=request.score_version,
            projectionVersion=request.projection_version,
            distinctAccountCount=len(accounts),
            distinctEventCount=evaluation.distinct_event_count,
            distinctPerformanceCount=evaluation.distinct_performance_count,
            verifiedRelationshipCount=sum(len(relation_scopes) for relation_scopes in by_type.values()),
            sourceScopes=source_scopes,
            availableSignals=list(evaluation.available_signals),
            excludedSignals=list(evaluation.excluded_signals),
            unavailableSignals=list(evaluation.unavailable_signals),
            normalSharingPossibilities=list(evaluation.normal_sharing_possibilities),
            scoreBreakdown=MultiEventScoreBreakdown(
                L=score.link, G=score.size, B=score.behavior, Q=score.quantity, R=score.recurrence,
                score=score.score, quantityStatus=score.quantity_status, quantityReason=score.quantity_reason,
            ),
            memberAccountIds=sorted(accounts),
            relationships=relationships,
        ))
    candidates.sort(key=lambda item: (-item.score_breakdown.score, item.candidate_id))
    return MultiEventAnalysisResponse(
        analysisId=request.analysis_id,
        ledgerVersionSetDigest=request.ledger_version_set_digest,
        scoreVersion=request.score_version,
        projectionScoreVersion=SCORE_VERSION_EXP2,
        projectionVersion=request.projection_version,
        algorithm="GDS_LOUVAIN",
        algorithmVersion=algorithm_version,
        candidates=candidates,
    )
