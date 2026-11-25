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
    return analyze_multi_event(request, communities, "gds-test;projection-v2.2-exp2-ipwindow-softhub;score-v2-exp2")


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
    assert result.algorithm_version.startswith("gds-test;")
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
