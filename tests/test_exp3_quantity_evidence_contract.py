from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas.exp3_quantity_evidence import (
    Exp3AccountQuantityEvidence,
    Exp3QuantityScopeManifest,
    Exp3RebookingEventRecord,
    validate_quantity_evidence_bundle,
)


AS_OF = "2026-11-28T00:00:00Z"
START = "2026-11-01T00:00:00Z"
END = "2026-11-28T00:00:00Z"


def _manifest(**overrides):
    payload = {
        "recordType": "EXP3_QUANTITY_SCOPE_MANIFEST",
        "contractVersion": "exp3-quantity-evidence-v1",
        "analysisId": str(uuid4()),
        "tenantId": str(uuid4()),
        "supplierNamespace": "supplier-a",
        "eventId": "event-a",
        "performanceId": str(uuid4()),
        "ledgerVersionId": str(uuid4()),
        "hmacPurpose": "TRACK1_CONNECTION",
        "hmacKeyVersion": "key-v1",
        "sourceVersion": "source-v1",
        "sourceAsOf": AS_OF,
        "analysisStartAt": START,
        "analysisEndAt": END,
        "ledgerCompleteSnapshot": True,
        "quantityDataStatus": "COMPLETE",
        "cancellationHandling": "NETTED",
        "duplicateHandling": "DEDUPLICATED",
        "duplicateSelection": "LATEST_REVISION_PER_TICKET_REFERENCE",
        "duplicateSourceRowsRejected": True,
        "policyVerificationStatus": "CONFIRMED",
        "policySnapshotId": str(uuid4()),
        "policyVersion": "policy-v1",
        "policySourceVersion": "source-v1",
        "confirmedPurchaseLimit": 4,
        "expectedAccountCount": 1,
        "expectedActiveSourceRowCount": 2,
        "expectedTicketUnitRowCount": 1,
        "expectedOrderLineRowCount": 1,
        "expectedOrderLineTicketQuantity": 3,
        "expectedActualTicketQuantity": 4,
        "rebookingEvidenceStatus": "COMPLETE",
        "expectedReservationEventCount": 0,
        "expectedCancellationEventCount": 0,
    }
    payload.update(overrides)
    return payload


def _evidence(manifest, **overrides):
    payload = {
        "recordType": "EXP3_ACCOUNT_QUANTITY_EVIDENCE",
        "contractVersion": "exp3-quantity-evidence-v1",
        "analysisId": manifest["analysisId"],
        "tenantId": manifest["tenantId"],
        "supplierNamespace": manifest["supplierNamespace"],
        "eventId": manifest["eventId"],
        "performanceId": manifest["performanceId"],
        "ledgerVersionId": manifest["ledgerVersionId"],
        "hmacPurpose": "TRACK1_CONNECTION",
        "hmacKeyVersion": "key-v1",
        "accountHmac": "a" * 64,
        "sourceVersion": "source-v1",
        "sourceAsOf": AS_OF,
        "analysisStartAt": START,
        "analysisEndAt": END,
        "activeSourceRowCount": 2,
        "ticketUnitRowCount": 1,
        "orderLineRowCount": 1,
        "orderLineTicketQuantity": 3,
        "actualTicketQuantity": 4,
    }
    payload.update(overrides)
    return payload


def test_bundle_accepts_ticket_and_order_line_units_and_keeps_only_account_hmac():
    manifest = Exp3QuantityScopeManifest.model_validate(_manifest())
    evidence = Exp3AccountQuantityEvidence.model_validate(_evidence(_manifest_from_model(manifest)))

    validate_quantity_evidence_bundle([manifest], [evidence])

    assert evidence.actual_ticket_quantity == 4
    assert manifest.source_quantity_is_complete
    assert manifest.q_can_use_verified_policy
    assert "accountHmac" in evidence.model_dump_json(by_alias=True)
    assert "accountId" not in evidence.model_dump_json(by_alias=True)


def _manifest_from_model(manifest):
    return manifest.model_dump(by_alias=True, mode="json")


def test_inconsistent_unit_total_is_rejected():
    with pytest.raises(ValidationError, match="quantity_account_total_mismatch"):
        Exp3AccountQuantityEvidence.model_validate(_evidence(_manifest(), actualTicketQuantity=5))


def test_confirmed_policy_requires_matching_source_and_provenance():
    with pytest.raises(ValidationError, match="quantity_policy_provenance_incomplete"):
        Exp3QuantityScopeManifest.model_validate(_manifest(policySourceVersion="different-source"))


def test_unconfirmed_policy_cannot_supply_a_limit():
    with pytest.raises(ValidationError, match="quantity_unconfirmed_limit"):
        Exp3QuantityScopeManifest.model_validate(_manifest(policyVerificationStatus="NOT_CONFIRMED"))


def test_incomplete_snapshot_or_policy_cannot_be_used_for_q():
    incomplete_source = Exp3QuantityScopeManifest.model_validate(
        _manifest(ledgerCompleteSnapshot=False)
    )
    unconfirmed_policy = Exp3QuantityScopeManifest.model_validate(
        _manifest(policyVerificationStatus="NOT_CONFIRMED", confirmedPurchaseLimit=None)
    )

    assert not incomplete_source.q_can_use_verified_policy
    assert not unconfirmed_policy.q_can_use_verified_policy


def test_bundle_rejects_missing_account_scope_rows_and_duplicate_account_rows():
    manifest = Exp3QuantityScopeManifest.model_validate(_manifest())
    evidence = Exp3AccountQuantityEvidence.model_validate(_evidence(_manifest_from_model(manifest)))

    with pytest.raises(ValueError, match="quantity_scope_coverage_mismatch"):
        validate_quantity_evidence_bundle([manifest], [])
    with pytest.raises(ValueError, match="duplicate_account_quantity_evidence"):
        validate_quantity_evidence_bundle([manifest], [evidence, evidence])


def test_bundle_rejects_tenant_or_analysis_scope_mixing():
    manifest = Exp3QuantityScopeManifest.model_validate(_manifest())
    evidence = Exp3AccountQuantityEvidence.model_validate(
        _evidence(_manifest_from_model(manifest), tenantId=str(uuid4()))
    )

    with pytest.raises(ValueError, match="quantity_evidence_scope_mismatch"):
        validate_quantity_evidence_bundle([manifest], [evidence])


def test_bundle_rejects_multiple_tenants_or_suppliers():
    first = Exp3QuantityScopeManifest.model_validate(_manifest())
    second = Exp3QuantityScopeManifest.model_validate(
        _manifest(
            analysisId=first.analysis_id,
            analysisStartAt=first.analysis_start_at.isoformat(),
            analysisEndAt=first.analysis_end_at.isoformat(),
            tenantId=str(uuid4()),
        )
    )

    with pytest.raises(ValueError, match="quantity_bundle_identity_mixed"):
        validate_quantity_evidence_bundle([first, second], [])


def test_analysis_period_must_not_exceed_source_as_of():
    with pytest.raises(ValidationError, match="invalid_quantity_analysis_period"):
        Exp3QuantityScopeManifest.model_validate(
            _manifest(analysisEndAt=datetime(2026, 12, 1, tzinfo=timezone.utc).isoformat())
        )


def test_empty_scope_is_represented_without_zero_quantity_account_rows():
    manifest = Exp3QuantityScopeManifest.model_validate(_manifest(
        sourceVersion=None, sourceAsOf=None, ledgerCompleteSnapshot=False, quantityDataStatus="UNKNOWN",
        cancellationHandling="UNKNOWN", duplicateHandling="UNKNOWN", duplicateSourceRowsRejected=False,
        policyVerificationStatus="UNKNOWN", policySnapshotId=None, policyVersion=None,
        policySourceVersion=None, confirmedPurchaseLimit=None, expectedAccountCount=0,
        expectedActiveSourceRowCount=0, expectedTicketUnitRowCount=0, expectedOrderLineRowCount=0,
        expectedOrderLineTicketQuantity=0, expectedActualTicketQuantity=0,
        rebookingEvidenceStatus="UNKNOWN",
    ))
    validate_quantity_evidence_bundle([manifest], [])
    assert not manifest.q_can_use_verified_policy


def test_rebooking_event_requires_opaque_ticket_and_real_cancellation_time():
    manifest = _manifest()
    event = {
        "recordType": "EXP3_REBOOKING_EVENT", "contractVersion": "exp3-quantity-evidence-v1",
        "analysisId": manifest["analysisId"], "tenantId": manifest["tenantId"],
        "supplierNamespace": manifest["supplierNamespace"], "eventId": manifest["eventId"],
        "performanceId": manifest["performanceId"], "ledgerVersionId": manifest["ledgerVersionId"],
        "hmacPurpose": "TRACK1_CONNECTION", "hmacKeyVersion": "key-v1",
        "accountHmac": "b" * 64, "ticketHmac": "c" * 64, "sourceVersion": "source-v1",
        "sourceAsOf": AS_OF, "analysisStartAt": START, "analysisEndAt": END,
        "eventType": "RESERVATION", "eventAt": None,
        "seatZone": "BALCONY", "seatRow": "R1", "seatNumber": "12",
    }
    parsed = Exp3RebookingEventRecord.model_validate(event)
    assert parsed.event_at is None
    with pytest.raises(ValidationError, match="rebooking_cancellation_time_required"):
        Exp3RebookingEventRecord.model_validate({**event, "eventType": "CANCELLED"})
    with pytest.raises(ValidationError, match="rebooking_event_time_outside_scope"):
        Exp3RebookingEventRecord.model_validate({**event, "eventAt": "2026-10-31T23:59:59Z"})
