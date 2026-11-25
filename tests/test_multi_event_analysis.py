from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas.multi_event import MultiEventAnalysisRequest
from services.multi_event_analysis import analyze_multi_event


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
        "scoreVersion": "score-v2-exp2-multi-event-recurrence",
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
    assert result.score_version == "score-v2-exp2-multi-event-recurrence"
    assert result.projection_score_version == "score-v2-exp2"
    assert result.projection_version == "projection-v2.2-exp2-ipwindow-softhub"
    assert _token("e") not in candidate.model_dump_json()


def test_payment_or_ip_only_never_link_cross_event_candidate():
    payload = _payload()
    first, second = payload["ledgerScopes"]
    payload["tickets"] = [
        _ticket(first, "a", "1", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
        _ticket(first, "b", "2", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
        _ticket(second, "a", "3", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
        _ticket(second, "b", "4", paymentHash=_token("b"), ipHash=_token("c"), ipNetworkType="RESIDENTIAL"),
    ]
    assert _analyze(payload).candidates == []


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
    assert result.candidates[0].verified_relationship_count == 2


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
