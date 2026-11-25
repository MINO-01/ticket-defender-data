"""고정 원장 범위를 위한 다공연 조사 후보 분석입니다.

회차별 exp1 및 exp2 경로를 변경하지 않습니다. 이 모듈은 고정된 여러 원장에서
검증된 안정 계정 참조와 희소한 강한 신호만 사용해 조사 후보를 제시합니다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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
)
from services.track1_scoring_exp2 import (
    DEFAULT_EXP2_PARAMETERS,
    SCORE_VERSION_EXP2,
    STRONG_RECURRENCE_SIGNALS,
    damping,
    priority_band_exp2,
    quantity_context,
    score_exp2,
)


@dataclass(frozen=True)
class _Evidence:
    signal: str
    token: str
    owners: frozenset[str]
    weight: float
    source_scope: MultiEventLedgerScope


def _candidate_id(analysis_id: str, accounts: Iterable[str]) -> str:
    raw = json.dumps([analysis_id, sorted(accounts)], separators=(",", ":"))
    return "multi-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scope_rows(request: MultiEventAnalysisRequest, keys: set[tuple[str, str, str]]) -> list[MultiEventLedgerScope]:
    return sorted((scope for scope in request.ledger_scopes if scope.key in keys), key=lambda scope: scope.key)


def _quantity_contexts(request: MultiEventAnalysisRequest):
    """Build one verified quantity context per account and fixed performance scope."""
    provenances: dict[tuple[tuple[str, str, str], str], set[tuple]] = defaultdict(set)
    uncertainties: dict[tuple[tuple[str, str, str], str], set[str]] = defaultdict(set)
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
    communities_by_scope: dict[tuple[str, str, str], list[frozenset[str]]],
    algorithm_version: str,
) -> MultiEventAnalysisResponse:
    """bounded inverted-index 분석을 수행하고 원본 token을 응답에 넣지 않습니다.

    입력 Pydantic 검증은 공급자 namespace, purpose, key version이 하나로 고정됐음을
    보장한다. 신호 union은 refund, phone, device, address로 제한한다. payment와 IP는
    공연 간 연결 조건으로 쓰지 않는다.
    """

    owner_tokens: dict[tuple[tuple[str, str, str], str, str], set[str]] = defaultdict(set)
    scope_accounts: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    scopes = {scope.key: scope for scope in request.ledger_scopes}
    for ticket in request.tickets:
        scope_accounts[ticket.scope_key].add(ticket.account_id)
        for signal in STRONG_RECURRENCE_SIGNALS:
            token = getattr(ticket, signal)
            if token is not None:
                owner_tokens[(ticket.scope_key, signal, token)].add(ticket.account_id)
    quantity_contexts = _quantity_contexts(request)

    evidence_by_token: dict[tuple[str, str], list[_Evidence]] = defaultdict(list)
    excluded_by_members: dict[frozenset[str], set[str]] = defaultdict(set)
    for (scope_key, signal, token), owners in sorted(owner_tokens.items()):
        if len(owners) < 2:
            continue
        factor = damping(signal, len(owners), max(1, len(scope_accounts[scope_key])))
        if factor is None:
            excluded_by_members[frozenset(owners)].add(signal)
            continue
        # Only retain account pairs that the full, per-performance GDS Louvain
        # result placed together. An in-memory component calculation is not a fallback.
        for community in communities_by_scope.get(scope_key, []):
            community_owners = owners.intersection(community)
            if len(community_owners) >= 2:
                member_set = frozenset(community_owners)
                evidence_by_token[(signal, token)].append(
                    _Evidence(signal, token, member_set, factor, scopes[scope_key])
                )

    # A stable shared token may recur with a partially changed membership set.
    # Keep only the exact account intersection across two distinct events. This
    # preserves the repeated relation without taking a transitive union.
    repeated_tokens: dict[tuple[str, frozenset[str]], list[_Evidence]] = defaultdict(list)
    for (signal, _token), observations in sorted(evidence_by_token.items()):
        by_event: dict[str, list[_Evidence]] = defaultdict(list)
        for item in observations:
            by_event[item.source_scope.event_id].append(item)
        event_ids = sorted(by_event)
        for left_index, left_event in enumerate(event_ids):
            for right_event in event_ids[left_index + 1:]:
                for left in by_event[left_event]:
                    for right in by_event[right_event]:
                        common = left.owners.intersection(right.owners)
                        if len(common) < 2:
                            continue
                        members = frozenset(common)
                        repeated_tokens[(signal, members)].extend((left, right))

    # A candidate is anchored to a repeated, verified signal group. We deliberately
    # do not take transitive closure across overlapping groups: that can create a
    # large false organization from a chain of otherwise separate shared signals.
    candidates_by_members: dict[frozenset[str], list[tuple[str, list[_Evidence]]]] = defaultdict(list)
    for (signal, members), observations in repeated_tokens.items():
        distinct_events = {item.source_scope.event_id for item in observations}
        if len(distinct_events) >= 2:
            candidates_by_members[members].append((signal, observations))

    candidates: list[MultiEventCandidate] = []
    for accounts, evidence_groups in sorted(candidates_by_members.items(), key=lambda item: tuple(sorted(item[0]))):
        related_by_identity = {}
        for _, observations in evidence_groups:
            for item in observations:
                related_by_identity[(item.signal, item.token, item.source_scope.key, item.owners)] = item
        related = [related_by_identity[key] for key in sorted(related_by_identity)]
        source_keys = {observation.source_scope.key for observation in related}
        source_scopes = _scope_rows(request, source_keys)
        event_ids = {scope.event_id for scope in source_scopes}
        performance_ids = {scope.performance_id for scope in source_scopes}
        if len(event_ids) < 2 or len(performance_ids) < 2:
            continue
        coverage: dict[str, float] = {}
        average_damping: dict[str, float] = {}
        for signal in sorted({signal for signal, _ in evidence_groups}):
            rows = [item for item in related if item.signal == signal]
            covered = set().union(*(item.owners for item in rows))
            coverage[signal] = min(len(covered) / len(accounts), 1.0)
            average_damping[signal] = sum(item.weight for item in rows) / len(rows)
        quantity = _quantity_for_candidate(accounts, source_scopes, quantity_contexts)
        score = score_exp2(
            len(accounts), coverage, average_damping, quantity, len(event_ids),
            excluded_signals=excluded_by_members.get(accounts, set()),
        )
        available = sorted(coverage)
        unavailable = sorted(set(STRONG_RECURRENCE_SIGNALS) - set(available))
        normal = sorted({
            "NORMAL_SHARED_POSSIBLE" if signal in {"refund_account_hash", "phone_hash", "address_hash", "device_id_hash"} else ""
            for signal in available
        } - {""})
        internal_relationships = []
        for signal in sorted({signal for signal, _ in evidence_groups}):
            observations = [item for item in related if item.signal == signal]
            relationship_scopes = {item.source_scope.key for item in observations}
            internal_relationships.append(MultiEventRelationship(
                signalType=signal,
                accountIds=sorted(accounts),
                sourceScopes=_scope_rows(request, relationship_scopes),
            ))
        distinct_relationships = {(signal, item.source_scope.key) for signal, observations in evidence_groups for item in observations}
        candidates.append(MultiEventCandidate(
            candidateId=_candidate_id(request.analysis_id, accounts),
            priorityBand=priority_band_exp2(score.score),
            scoreVersion=request.score_version,
            projectionVersion=request.projection_version,
            distinctAccountCount=len(accounts),
            distinctEventCount=len(event_ids),
            distinctPerformanceCount=len(performance_ids),
            verifiedRelationshipCount=len(distinct_relationships),
            sourceScopes=source_scopes,
            availableSignals=available,
            excludedSignals=list(score.excluded_signals),
            unavailableSignals=unavailable,
            normalSharingPossibilities=normal,
            scoreBreakdown=MultiEventScoreBreakdown(
                L=score.link, G=score.size, B=score.behavior, Q=score.quantity, R=score.recurrence,
                score=score.score, quantityStatus=score.quantity_status, quantityReason=score.quantity_reason,
            ),
            memberAccountIds=sorted(accounts),
            relationships=internal_relationships,
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
