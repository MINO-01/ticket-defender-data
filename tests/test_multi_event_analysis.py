from datetime import datetime, timezone
import hashlib
import math
from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas.multi_event import MultiEventAnalysisRequest
from services.multi_event_analysis import analyze_multi_event
from services.multi_event_candidates import (
    RecurrenceComparisonLimit,
    cocluster_comparison_count,
    require_comparison_budget,
)


def _token(value: str) -> str:
    return value * 64


def _payload():
    first, second = str(uuid4()), str(uuid4())
    return {
        "analysisId": str(uuid4()),
        "tenantId": str(uuid4()),
        "ledgerVersionSetDigest": _token("d"),
        "analysisAsOf": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "analysisStartAt": datetime(2025, 12, 1, tzinfo=timezone.utc).isoformat(),
        "analysisEndAt": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "scoreVersion": "score-v2-exp2-multi-performance-recurrence",
        "projectionVersion": "projection-v2.2-exp2-ipwindow-softhub",
        "ledgerScopes": [
            {"eventId": "event-a", "performanceId": first, "ledgerVersionId": str(uuid4()), "sourceBatchId": "batch-a"},
            {"eventId": "event-b", "performanceId": second, "ledgerVersionId": str(uuid4()), "sourceBatchId": "batch-b"},
        ],
        "tickets": [],
    }


def _ticket(scope, account, reservation, **signals):
    return {
        "accountId": _token(account),
        "reservationNo": _token(reservation),
        "eventId": scope["eventId"],
        "performanceId": scope["performanceId"],
        "ledgerVersionId": scope["ledgerVersionId"],
        "supplierNamespace": "supplier-a",
        "hmacPurpose": "TRACK1_CONNECTION",
        "hmacKeyVersion": "v1",
        "sourceVersion": "source-v1",
        "purchasedAt": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "effectiveTicketQuantity": 1,
        **signals,
    }


def _analyze(payload):
    request = MultiEventAnalysisRequest.model_validate(payload)
    communities = {}
    for scope in request.ledger_scopes:
        members = frozenset(ticket.account_id for ticket in request.tickets if ticket.scope_key == scope.key)
        communities[scope.key] = [members] if len(members) >= 2 else []
    return analyze_multi_event(request, communities, "gds-test")


def test_multi_event_candidate_uses_verified_strong_signal_without_exposing_token():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    payload["tickets"] = [
        _ticket(first, "a", "1", refundAccountHash=_token("e")),
        _ticket(first, "b", "2", refundAccountHash=_token("e")),
        _ticket(second, "a", "3", refundAccountHash=_token("e")),
        _ticket(second, "b", "4", refundAccountHash=_token("e")),
    ]
    result = _analyze(payload)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.distinct_event_count == 2
    assert candidate.distinct_performance_count == 2
    assert candidate.available_signals == ["refund_account_hash"]
    assert result.algorithm == "GDS_LOUVAIN"
    assert result.algorithm_version == "gds-test"
    assert result.score_version == "score-v2-exp2-multi-performance-recurrence"
    assert result.projection_score_version == "score-v2-exp2"
    assert result.projection_version == "projection-v2.2-exp2-ipwindow-softhub"
    assert _token("e") not in candidate.model_dump_json()


def test_cancelled_tickets_are_optional_separate_evidence_and_do_not_change_exp2_result():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    payload["tickets"] = [
        _ticket(first, "a", "1", phoneHash=_token("e")),
        _ticket(first, "b", "2", phoneHash=_token("e")),
        _ticket(second, "a", "3", phoneHash=_token("e")),
        _ticket(second, "b", "4", phoneHash=_token("e")),
    ]
    without_cancelled = _analyze(payload)
    payload["cancelledTickets"] = [{
        "accountHmac": _token("a"), "ticketHmac": _token("b"),
        "eventId": first["eventId"], "performanceId": first["performanceId"],
        "ledgerVersionId": first["ledgerVersionId"], "supplierNamespace": "supplier-a",
        "hmacPurpose": "TRACK1_CONNECTION", "hmacKeyVersion": "v1",
        "sourceVersion": "cancel-source-v1", "sourceRowOrdinal": 99,
        "seatZone": "BALCONY", "seatRow": "R1", "seatNumber": "12",
        "cancelledAt": "2025-12-20T10:00:00Z",
    }]
    with_cancelled = _analyze(payload)
    assert with_cancelled.model_dump(mode="json", by_alias=True) == without_cancelled.model_dump(mode="json", by_alias=True)


def test_active_ticket_v3_metadata_is_optional_and_does_not_change_exp2_result():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    tickets = [
        _ticket(first, "a", "1", phoneHash=_token("e")),
        _ticket(first, "b", "2", phoneHash=_token("e")),
        _ticket(second, "a", "3", phoneHash=_token("e")),
        _ticket(second, "b", "4", phoneHash=_token("e")),
    ]
    payload["tickets"] = tickets
    baseline = _analyze(payload)

    for ticket in tickets:
        ticket.update({
            "seatZone": "BALCONY", "seatRow": "R1", "seatNumber": "12",
            "reservedAt": "2025-12-20T09:00:00Z", "paidAt": "2025-12-20T09:05:00Z",
            "accountCreatedAt": "2024-01-03T00:00:00Z",
        })
    enriched = _analyze(payload)

    assert enriched.model_dump(mode="json", by_alias=True) == baseline.model_dump(mode="json", by_alias=True)


def test_active_ticket_v3_metadata_keeps_bounds_and_aware_datetime_validation():
    payload = _payload()
    first, _second = payload["ledgerScopes"]
    payload["tickets"] = [_ticket(first, "a", "1", phoneHash=_token("e"), seatZone="x" * 65)]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)

    payload["tickets"] = [_ticket(first, "a", "1", phoneHash=_token("e"), reservedAt="2025-12-20T09:00:00")]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)


def test_cancelled_ticket_scope_duplicate_and_extra_fields_are_rejected():
    payload = _payload()
    first, _second = payload["ledgerScopes"]
    payload["tickets"] = [_ticket(first, "a", "1", phoneHash=_token("e"))]
    cancelled = {
        "accountHmac": _token("a"), "ticketHmac": _token("b"),
        "eventId": first["eventId"], "performanceId": first["performanceId"],
        "ledgerVersionId": first["ledgerVersionId"], "supplierNamespace": "supplier-a",
        "hmacPurpose": "TRACK1_CONNECTION", "hmacKeyVersion": "v1",
        "sourceVersion": "cancel-source-v1", "sourceRowOrdinal": 99,
        "cancelledAt": "2025-12-20T10:00:00Z",
    }
    payload["cancelledTickets"] = [cancelled, cancelled]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)

    payload["cancelledTickets"] = [{**cancelled, "eventId": "unlisted-event"}]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)

    payload["cancelledTickets"] = [{**cancelled, "rawTicketNumber": "must-not-be-accepted"}]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)


def test_payment_or_ip_only_without_repeated_cocluster_never_links_candidate():
    """같은 계정이 결제와 IP 시간창만 공유해도 두 회차에서 같은 군집이 아니면 후보가 아닙니다."""
    payload = _payload()
    first, second = payload["ledgerScopes"]
    payload["tickets"] = [
        _ticket(first, "a", "1", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
        _ticket(first, "b", "2", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
        _ticket(second, "a", "3", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
        _ticket(second, "b", "4", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
    ]
    request = MultiEventAnalysisRequest.model_validate(payload)
    communities = {
        request.ledger_scopes[0].key: [frozenset({_token("a"), _token("b")})],
        # 두 번째 회차의 GDS Louvain은 두 계정을 서로 다른 군집에 두었습니다.
        request.ledger_scopes[1].key: [frozenset({_token("a")}), frozenset({_token("b")})],
    }
    assert analyze_multi_event(request, communities, "gds-test").candidates == []


def test_overlapping_repeated_relationships_do_not_transitively_merge_groups():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    third = {**second, "eventId": "event-c", "performanceId": str(uuid4()), "ledgerVersionId": str(uuid4())}
    payload["ledgerScopes"].append(third)
    payload["tickets"] = [
        _ticket(first, "a", "1", phoneHash=_token("e")),
        _ticket(first, "b", "2", phoneHash=_token("e")),
        _ticket(second, "a", "3", phoneHash=_token("e")),
        _ticket(second, "b", "4", phoneHash=_token("e")),
        _ticket(second, "b", "5", deviceIdHash=_token("d")),
        _ticket(second, "c", "6", deviceIdHash=_token("d")),
        _ticket(third, "b", "7", deviceIdHash=_token("d")),
        _ticket(third, "c", "8", deviceIdHash=_token("d")),
    ]
    result = _analyze(payload)
    assert {frozenset(candidate.member_account_ids) for candidate in result.candidates} == {
        frozenset({_token("a"), _token("b")}),
        frozenset({_token("b"), _token("c")}),
    }


def test_partial_membership_recurrence_keeps_only_stable_shared_accounts():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    payload["tickets"] = [
        _ticket(first, "a", "1", refundAccountHash=_token("e")),
        _ticket(first, "b", "2", refundAccountHash=_token("e")),
        _ticket(first, "c", "3", refundAccountHash=_token("e")),
        _ticket(second, "a", "4", refundAccountHash=_token("e")),
        _ticket(second, "b", "5", refundAccountHash=_token("e")),
    ]
    request = MultiEventAnalysisRequest.model_validate(payload)
    communities = {
        request.ledger_scopes[0].key: [frozenset({_token("a"), _token("b"), _token("c")})],
        request.ledger_scopes[1].key: [frozenset({_token("a"), _token("b")})],
    }

    result = analyze_multi_event(request, communities, "gds-test")

    assert len(result.candidates) == 1
    assert set(result.candidates[0].member_account_ids) == {_token("a"), _token("b")}
    assert result.candidates[0].distinct_event_count == 2
    # 환불 계좌 반복 2개 회차와 같은 계정 반복 동시 군집 2개 회차가 각각 근거로 남습니다.
    assert result.candidates[0].verified_relationship_count == 4
    assert [relation.signal_type for relation in result.candidates[0].relationships] == [
        "refund_account_hash", "stable_account_cocluster",
    ]


def test_quantity_component_uses_snapshot_bound_verified_performance_quantities():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    first["policySnapshotId"] = str(uuid4())
    second["policySnapshotId"] = str(uuid4())
    payload["tickets"] = [
        _ticket(first, "a", "1", refundAccountHash=_token("e"),
                effectiveTicketQuantity=1, confirmedPurchaseLimit=2,
                quantityPolicySnapshotId=first["policySnapshotId"], quantityPolicyVersion="policy-v1",
                quantitySourceVersion="source-v1", quantityStatus="COMPLETE"),
        _ticket(first, "b", "2", refundAccountHash=_token("e"),
                effectiveTicketQuantity=2, confirmedPurchaseLimit=2,
                quantityPolicySnapshotId=first["policySnapshotId"], quantityPolicyVersion="policy-v1",
                quantitySourceVersion="source-v1", quantityStatus="COMPLETE"),
        _ticket(second, "a", "3", refundAccountHash=_token("e"),
                effectiveTicketQuantity=1, confirmedPurchaseLimit=2,
                quantityPolicySnapshotId=second["policySnapshotId"], quantityPolicyVersion="policy-v2",
                quantitySourceVersion="source-v1", quantityStatus="COMPLETE"),
        _ticket(second, "b", "4", refundAccountHash=_token("e"),
                effectiveTicketQuantity=2, confirmedPurchaseLimit=2,
                quantityPolicySnapshotId=second["policySnapshotId"], quantityPolicyVersion="policy-v2",
                quantitySourceVersion="source-v1", quantityStatus="COMPLETE"),
    ]

    result = _analyze(payload)

    assert result.candidates[0].score_breakdown.quantity == 0.75
    assert result.candidates[0].score_breakdown.quantity_status == "VERIFIED"
    assert result.candidates[0].score_breakdown.quantity_reason == "POLICY_SNAPSHOT_AND_QUANTITY_VERIFIED"


def test_quantity_source_mismatch_uses_neutral_value_and_explains_uncertainty():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    first["policySnapshotId"] = str(uuid4())
    second["policySnapshotId"] = str(uuid4())
    payload["tickets"] = [
        _ticket(scope, account, reservation, refundAccountHash=_token("e"),
                effectiveTicketQuantity=1, confirmedPurchaseLimit=2,
                quantityPolicySnapshotId=scope["policySnapshotId"], quantityPolicyVersion="policy-v1",
                quantitySourceVersion="different-source", quantityStatus="COMPLETE")
        for scope, account, reservation in ((first, "a", "1"), (first, "b", "2"),
                                            (second, "a", "3"), (second, "b", "4"))
    ]

    result = _analyze(payload)

    assert result.candidates[0].score_breakdown.quantity == 0.5
    assert result.candidates[0].score_breakdown.quantity_status == "NEUTRAL_NO_POLICY"
    assert "QUANTITY_POLICY_SOURCE_MISMATCH" in result.candidates[0].score_breakdown.quantity_reason


def test_mixed_supplier_or_key_version_is_rejected_before_analysis():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    left = _ticket(first, "a", "1", deviceIdHash=_token("d"))
    right = _ticket(second, "b", "2", deviceIdHash=_token("d"))
    right["supplierNamespace"] = "supplier-b"
    payload["tickets"] = [left, right]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)


def test_ticket_outside_fixed_ledger_scope_is_rejected():
    payload = _payload()
    first = payload["ledgerScopes"][0]
    row = _ticket(first, "a", "1", phoneHash=_token("e"))
    row["ledgerVersionId"] = str(uuid4())
    payload["tickets"] = [row]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)


def test_multiple_ticket_rows_for_one_reservation_are_distinct_but_account_relation_is_deduplicated():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    payload["tickets"] = [
        _ticket(first, "a", "1", sourceRowOrdinal=1, refundAccountHash=_token("e")),
        _ticket(first, "a", "1", sourceRowOrdinal=2, refundAccountHash=_token("e")),
        _ticket(first, "b", "2", sourceRowOrdinal=3, refundAccountHash=_token("e")),
        _ticket(second, "a", "3", sourceRowOrdinal=1, refundAccountHash=_token("e")),
        _ticket(second, "b", "4", sourceRowOrdinal=2, refundAccountHash=_token("e")),
    ]
    request = MultiEventAnalysisRequest.model_validate(payload)
    communities = {scope.key: [frozenset({_token("a"), _token("b")})] for scope in request.ledger_scopes}

    result = analyze_multi_event(request, communities, "gds-test")

    assert len(result.candidates) == 1
    assert set(result.candidates[0].member_account_ids) == {_token("a"), _token("b")}
    assert result.candidates[0].distinct_account_count == 2


def test_source_row_ordinal_must_be_unique_within_ledger_scope():
    payload = _payload()
    first = payload["ledgerScopes"][0]
    payload["tickets"] = [
        _ticket(first, "a", "reservation-a", sourceRowOrdinal=1, phoneHash=_token("p")),
        _ticket(first, "b", "reservation-b", sourceRowOrdinal=1, phoneHash=_token("p")),
    ]

    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)


# 다회차(회차 단위) 후보 생성, 같은 계정 반복 동시 군집, R 정정에 대한 회귀 테스트입니다.

def _scope(event_id: str) -> dict:
    return {"eventId": event_id, "performanceId": str(uuid4()), "ledgerVersionId": str(uuid4()),
            "sourceBatchId": f"batch-{uuid4().hex[:8]}"}


def _hex(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _row(scope, account, reservation_key, **signals):
    """한 글자 계정은 기존 token 규칙을, 그 밖의 계정과 예약 키는 SHA-256 가명값을 씁니다."""
    row = _ticket(scope, "0", "0", **signals)
    row["accountId"] = _token(account) if len(account) == 1 else _hex(account)
    row["reservationNo"] = _hex(f"{scope['performanceId']}|{reservation_key}")
    return row


def _payload_with(scopes):
    payload = _payload()
    payload["ledgerScopes"] = scopes
    return payload


def _expected_score(member_count, components, performance_count, quantity=0.5):
    """사전 등록 산식을 테스트 오라클로만 다시 계산합니다."""
    product = 1.0
    for value in components:
        product *= 1.0 - value
    link = 1.0 - product
    size = 0.6 + 0.4 * min(math.log2(member_count) / 5, 1.0)
    behavior = 0.5 * quantity + 0.5 * min((performance_count - 1) / 3, 1.0)
    return link * size * behavior


def test_same_event_four_performances_with_shared_device_is_one_candidate_with_full_recurrence():
    scopes = [_scope("event-a") for _ in range(4)]
    payload = _payload_with(scopes)
    payload["tickets"] = [
        _row(scope, account, f"{index}{account}", deviceIdHash=_token("d"))
        for index, scope in enumerate(scopes) for account in ("a", "b")
    ]
    result = _analyze(payload)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.distinct_event_count == 1
    assert candidate.distinct_performance_count == 4
    assert candidate.score_breakdown.recurrence == 1.0
    assert candidate.available_signals == ["device_id_hash"]
    assert candidate.score_breakdown.score == pytest.approx(_expected_score(2, [0.6], 4), abs=1e-4)
    assert {relation.signal_type for relation in candidate.relationships} == {"device_id_hash", "stable_account_cocluster"}


def test_one_event_one_performance_is_rejected_before_analysis():
    payload = _payload_with([_scope("event-a")])
    payload["tickets"] = [_row(payload["ledgerScopes"][0], "a", "1", deviceIdHash=_token("d"))]
    with pytest.raises(ValidationError):
        MultiEventAnalysisRequest.model_validate(payload)


def test_same_performance_in_two_ledger_scopes_is_not_recurrence():
    first = _scope("event-a")
    second = {**first, "ledgerVersionId": str(uuid4())}
    payload = _payload_with([first, second])
    payload["tickets"] = [_row(first, "a", "1", deviceIdHash=_token("d"))]
    with pytest.raises(ValidationError) as error:
        MultiEventAnalysisRequest.model_validate(payload)
    assert error.value.errors()[0]["type"] == "multi_event_scope_required"


def test_same_accounts_repeatedly_coclustered_by_ip_window_only_is_stable_account_cocluster_candidate():
    scopes = [_scope("event-a"), _scope("event-b")]
    payload = _payload_with(scopes)
    payload["tickets"] = [
        _row(scope, account, f"{index}{account}", ipHash=_token("c"), ipNetworkType="CORPORATE_EDU")
        for index, scope in enumerate(scopes) for account in ("a", "b", "c")
    ]
    result = _analyze(payload)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert [relation.signal_type for relation in candidate.relationships] == ["stable_account_cocluster"]
    assert candidate.available_signals == ["ip_window:CORPORATE_EDU"]
    assert candidate.distinct_performance_count == 2
    assert candidate.score_breakdown.link == pytest.approx(0.5)
    assert candidate.score_breakdown.score == pytest.approx(0.1514, abs=1e-4)
    assert "ip_window" not in candidate.unavailable_signals
    for member in candidate.member_reasons:
        recurrence = next(reason for reason in member.reasons if reason.code == "REPEATED_PERFORMANCES")
        assert recurrence.peer_count == 0
        assert set(recurrence.performances) == {scope["performanceId"] for scope in scopes}


def test_two_events_four_performances_with_shared_refund_account_has_full_recurrence():
    scopes = [_scope("event-a"), _scope("event-a"), _scope("event-b"), _scope("event-b")]
    payload = _payload_with(scopes)
    payload["tickets"] = [
        _row(scope, account, f"{index}{account}", refundAccountHash=_token("e"))
        for index, scope in enumerate(scopes) for account in ("a", "b")
    ]
    candidate = _analyze(payload).candidates[0]
    assert candidate.distinct_event_count == 2
    assert candidate.distinct_performance_count == 4
    assert candidate.score_breakdown.recurrence == 1.0
    assert candidate.score_breakdown.score == pytest.approx(0.408, abs=1e-4)
    assert candidate.priority_band == "HIGH"


def test_group_seen_in_only_one_performance_is_not_a_candidate():
    scopes = [_scope("event-a"), _scope("event-a")]
    payload = _payload_with(scopes)
    payload["tickets"] = [
        _row(scopes[0], "a", "1", refundAccountHash=_token("e"), deviceIdHash=_token("d")),
        _row(scopes[0], "b", "2", refundAccountHash=_token("e"), deviceIdHash=_token("d")),
        _row(scopes[1], "c", "3", phoneHash=_token("f")),
        _row(scopes[1], "d", "4", phoneHash=_token("f")),
    ]
    assert _analyze(payload).candidates == []


@pytest.mark.parametrize("second_accounts", [("d", "e", "f"), ("c", "d", "e")])
def test_dormitory_counterexample_with_different_accounts_per_performance_is_not_a_candidate(second_accounts):
    scopes = [_scope("event-a"), _scope("event-b")]
    payload = _payload_with(scopes)
    payload["tickets"] = [
        _row(scopes[0], account, f"0{account}", ipHash=_token("c"), ipNetworkType="CORPORATE_EDU")
        for account in ("a", "b", "c")
    ] + [
        _row(scopes[1], account, f"1{account}", ipHash=_token("c"), ipNetworkType="CORPORATE_EDU")
        for account in second_accounts
    ]
    # 교집합이 2계정 미만이면 같은 IP 시간창이 반복돼도 후보를 만들지 않습니다.
    assert _analyze(payload).candidates == []


def test_overlapping_coclusters_are_not_transitively_merged():
    scopes = [_scope("event-a"), _scope("event-a"), _scope("event-b")]
    payload = _payload_with(scopes)
    memberships = (("a", "b"), ("a", "b", "c"), ("b", "c"))
    payload["tickets"] = [
        _row(scope, account, f"{index}{account}", ipHash=_token("c"), ipNetworkType="RESIDENTIAL")
        for index, (scope, accounts) in enumerate(zip(scopes, memberships))
        for account in accounts
    ]
    result = _analyze(payload)
    members = {frozenset(candidate.member_account_ids) for candidate in result.candidates}
    # A∩B와 B∩C는 각각 후보이고, 두 후보를 합친 {a,b,c} 3회차 후보는 만들지 않습니다.
    assert members == {frozenset({_token("a"), _token("b")}), frozenset({_token("b"), _token("c")})}
    assert all(candidate.distinct_performance_count == 2 for candidate in result.candidates)


@pytest.mark.parametrize("field,value", [("supplierNamespace", "supplier-b"), ("hmacKeyVersion", "v2")])
def test_mixed_supplier_or_key_version_is_rejected(field, value):
    scopes = [_scope("event-a"), _scope("event-a")]
    payload = _payload_with(scopes)
    left = _row(scopes[0], "a", "1", deviceIdHash=_token("d"))
    right = _row(scopes[1], "a", "2", deviceIdHash=_token("d"))
    right[field] = value
    payload["tickets"] = [left, right]
    with pytest.raises(ValidationError) as error:
        MultiEventAnalysisRequest.model_validate(payload)
    assert error.value.errors()[0]["type"] == "identity_contract_mixed"


def test_response_json_never_contains_signal_token_values():
    scopes = [_scope("event-a"), _scope("event-b")]
    payload = _payload_with(scopes)
    tokens = {"refundAccountHash": _token("e"), "phoneHash": _token("f"), "deviceIdHash": _token("1"),
              "addressHash": _token("9"), "paymentHash": _token("8"), "ipHash": _token("7")}
    payload["tickets"] = [
        _row(scope, account, f"{index}{account}", ipNetworkType="RESIDENTIAL", **tokens)
        for index, scope in enumerate(scopes) for account in ("a", "b")
    ]
    serialized = _analyze(payload).model_dump_json(by_alias=True)
    assert "candidateId" in serialized
    for value in tokens.values():
        assert value not in serialized


@pytest.mark.parametrize("signals,components,expected", [
    ({"paymentHash": "8" * 64}, [0.2], 0.0606),
    ({"paymentHash": "8" * 64, "ipHash": "7" * 64, "ipNetworkType": "RESIDENTIAL"}, [0.2, 0.5], 0.1817),
])
def test_family_with_same_card_three_accounts_two_performances_is_low_priority(signals, components, expected):
    """가족형 반례: 같은 카드를 쓰는 같은 계정 3개가 두 회차에서 같은 군집이면 후보는 되지만 점수는 낮습니다."""
    scopes = [_scope("event-a"), _scope("event-b")]
    payload = _payload_with(scopes)
    payload["tickets"] = [
        _row(scope, account, f"{index}{account}", **signals)
        for index, scope in enumerate(scopes) for account in ("a", "b", "c")
    ]
    result = _analyze(payload)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert [relation.signal_type for relation in candidate.relationships] == ["stable_account_cocluster"]
    assert candidate.score_breakdown.score == pytest.approx(expected, abs=1e-4)
    assert candidate.score_breakdown.score == pytest.approx(_expected_score(3, components, 2), abs=1e-4)
    assert candidate.priority_band == "LOW"
    assert candidate.normal_sharing_possibilities == ["NORMAL_SHARED_POSSIBLE"]


def test_retired_score_version_is_rejected_for_new_requests():
    payload = _payload()
    payload["scoreVersion"] = "score-v2-exp2-multi-event-recurrence"
    payload["tickets"] = [_row(payload["ledgerScopes"][0], "a", "1", phoneHash=_token("e"))]
    with pytest.raises(ValidationError) as error:
        MultiEventAnalysisRequest.model_validate(payload)
    assert error.value.errors()[0]["type"] == "SCORE_VERSION_RETIRED"


def test_hub_excluded_shared_signal_is_reported_without_link_contribution():
    scopes = [_scope("event-a"), _scope("event-b")]
    payload = _payload_with(scopes)
    rows = []
    for index, scope in enumerate(scopes):
        rows += [_row(scope, account, f"{index}{account}", refundAccountHash=_token("e"),
                         ipHash=_token("7"), ipNetworkType="MOBILE_CGNAT") for account in ("a", "b")]
        # MOBILE_CGNAT IP 시간창은 9계정 이상에서 hard hub로 제외됩니다.
        rows += [_row(scope, f"filler-{filler}", f"{index}-{filler}", ipHash=_token("7"), ipNetworkType="MOBILE_CGNAT")
                 for filler in range(10, 17)]
    payload["tickets"] = rows
    request = MultiEventAnalysisRequest.model_validate(payload)
    communities = {scope.key: [frozenset({_token("a"), _token("b")})] for scope in request.ledger_scopes}
    candidate = analyze_multi_event(request, communities, "gds-test").candidates[0]
    assert candidate.available_signals == ["refund_account_hash"]
    assert candidate.excluded_signals == ["ip_window:MOBILE_CGNAT"]


def test_cocluster_comparison_budget_fails_instead_of_truncating():
    assert cocluster_comparison_count([1, 2, 3, 500]) == 0 + 1 + 3 + 124_750
    require_comparison_budget(2_000_000)
    with pytest.raises(RecurrenceComparisonLimit) as error:
        require_comparison_budget(2_000_001)
    assert error.value.code == "RECURRENCE_COMPARISON_LIMIT_EXCEEDED"
