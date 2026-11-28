"""Versioned exp3 quantity evidence contract.

This contract is intentionally separate from the exp2 multi-event request. It
describes complete, account/performance quantity evidence without enabling a
score or treating a purchase limit as an actual purchase quantity.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from schemas.dto import HashToken, UUIDText


ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class Exp3QuantityScopeManifest(BaseModel):
    """One immutable manifest per tenant/supplier/event/performance/version scope."""

    model_config = ConfigDict(extra="forbid")

    record_type: Literal["EXP3_QUANTITY_SCOPE_MANIFEST"] = Field(alias="recordType")
    contract_version: Literal["exp3-quantity-evidence-v1"] = Field(alias="contractVersion")
    analysis_id: UUIDText = Field(alias="analysisId")
    tenant_id: UUIDText = Field(alias="tenantId")
    supplier_namespace: ShortText = Field(alias="supplierNamespace")
    event_id: ShortText = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    ledger_version_id: UUIDText = Field(alias="ledgerVersionId")
    hmac_purpose: Literal["TRACK1_CONNECTION"] = Field(alias="hmacPurpose")
    hmac_key_version: ShortText = Field(alias="hmacKeyVersion")
    source_version: ShortText = Field(alias="sourceVersion")
    source_as_of: AwareDatetime = Field(alias="sourceAsOf")
    analysis_start_at: AwareDatetime = Field(alias="analysisStartAt")
    analysis_end_at: AwareDatetime = Field(alias="analysisEndAt")
    ledger_complete_snapshot: StrictBool = Field(alias="ledgerCompleteSnapshot")
    quantity_data_status: Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"] = Field(alias="quantityDataStatus")
    cancellation_handling: Literal["NETTED", "GROSS", "UNKNOWN"] = Field(alias="cancellationHandling")
    duplicate_handling: Literal["DEDUPLICATED", "INCOMPLETE", "UNKNOWN"] = Field(alias="duplicateHandling")
    duplicate_selection: Literal["LATEST_REVISION_PER_TICKET_REFERENCE"] = Field(alias="duplicateSelection")
    duplicate_source_rows_rejected: StrictBool = Field(alias="duplicateSourceRowsRejected")
    policy_verification_status: Literal["CONFIRMED", "NOT_CONFIRMED", "UNKNOWN"] = Field(alias="policyVerificationStatus")
    policy_snapshot_id: UUIDText | None = Field(default=None, alias="policySnapshotId")
    policy_version: ShortText | None = Field(default=None, alias="policyVersion")
    policy_source_version: ShortText | None = Field(default=None, alias="policySourceVersion")
    confirmed_purchase_limit: StrictInt | None = Field(default=None, alias="confirmedPurchaseLimit", ge=1, le=100)
    expected_account_count: StrictInt = Field(alias="expectedAccountCount", ge=1, le=400_000)
    expected_active_source_row_count: StrictInt = Field(alias="expectedActiveSourceRowCount", ge=1, le=400_000)
    expected_ticket_unit_row_count: StrictInt = Field(alias="expectedTicketUnitRowCount", ge=0, le=400_000)
    expected_order_line_row_count: StrictInt = Field(alias="expectedOrderLineRowCount", ge=0, le=400_000)
    expected_order_line_ticket_quantity: StrictInt = Field(alias="expectedOrderLineTicketQuantity", ge=0, le=40_000_000)
    expected_actual_ticket_quantity: StrictInt = Field(alias="expectedActualTicketQuantity", ge=1, le=40_000_000)

    @model_validator(mode="after")
    def validate_manifest(self):
        if self.analysis_end_at <= self.analysis_start_at or self.analysis_end_at > self.source_as_of:
            raise PydanticCustomError("invalid_quantity_analysis_period", "Quantity analysis period must be ordered and not after sourceAsOf")
        if self.expected_active_source_row_count != self.expected_ticket_unit_row_count + self.expected_order_line_row_count:
            raise PydanticCustomError("quantity_scope_row_count_mismatch", "Active source rows must equal TICKET and ORDER_LINE row counts")
        if self.expected_actual_ticket_quantity != self.expected_ticket_unit_row_count + self.expected_order_line_ticket_quantity:
            raise PydanticCustomError("quantity_scope_total_mismatch", "Actual ticket quantity must equal TICKET rows plus ORDER_LINE quantity")
        if self.expected_order_line_row_count == 0 and self.expected_order_line_ticket_quantity != 0:
            raise PydanticCustomError("quantity_scope_unit_mismatch", "ORDER_LINE quantity requires at least one ORDER_LINE row")
        if self.expected_order_line_row_count > 0 and self.expected_order_line_ticket_quantity < self.expected_order_line_row_count:
            raise PydanticCustomError("quantity_scope_unit_mismatch", "Each ORDER_LINE row must represent at least one ticket")
        if self.policy_verification_status == "CONFIRMED":
            if (self.policy_snapshot_id is None or self.policy_version is None
                    or self.policy_source_version != self.source_version or self.confirmed_purchase_limit is None):
                raise PydanticCustomError("quantity_policy_provenance_incomplete", "Confirmed policy must bind a limit, snapshot, version, and matching source")
        elif self.confirmed_purchase_limit is not None:
            raise PydanticCustomError("quantity_unconfirmed_limit", "An unconfirmed policy cannot carry a purchase limit")
        return self

    @property
    def scope_key(self) -> tuple[str, str, str, str, str, str]:
        return (self.analysis_id, self.tenant_id, self.supplier_namespace, self.event_id,
                self.performance_id, self.ledger_version_id)

    @property
    def source_quantity_is_complete(self) -> bool:
        return (self.ledger_complete_snapshot and self.quantity_data_status == "COMPLETE"
                and self.cancellation_handling == "NETTED" and self.duplicate_handling == "DEDUPLICATED"
                and self.duplicate_source_rows_rejected)

    @property
    def q_can_use_verified_policy(self) -> bool:
        return (self.source_quantity_is_complete and self.policy_verification_status == "CONFIRMED"
                and self.confirmed_purchase_limit is not None)


class Exp3AccountQuantityEvidence(BaseModel):
    """Actual active quantity for one pseudonymous account and fixed performance scope."""

    model_config = ConfigDict(extra="forbid")

    record_type: Literal["EXP3_ACCOUNT_QUANTITY_EVIDENCE"] = Field(alias="recordType")
    contract_version: Literal["exp3-quantity-evidence-v1"] = Field(alias="contractVersion")
    analysis_id: UUIDText = Field(alias="analysisId")
    tenant_id: UUIDText = Field(alias="tenantId")
    supplier_namespace: ShortText = Field(alias="supplierNamespace")
    event_id: ShortText = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    ledger_version_id: UUIDText = Field(alias="ledgerVersionId")
    hmac_purpose: Literal["TRACK1_CONNECTION"] = Field(alias="hmacPurpose")
    hmac_key_version: ShortText = Field(alias="hmacKeyVersion")
    account_hmac: HashToken = Field(alias="accountHmac")
    source_version: ShortText = Field(alias="sourceVersion")
    source_as_of: AwareDatetime = Field(alias="sourceAsOf")
    analysis_start_at: AwareDatetime = Field(alias="analysisStartAt")
    analysis_end_at: AwareDatetime = Field(alias="analysisEndAt")
    active_source_row_count: StrictInt = Field(alias="activeSourceRowCount", ge=1, le=400_000)
    ticket_unit_row_count: StrictInt = Field(alias="ticketUnitRowCount", ge=0, le=400_000)
    order_line_row_count: StrictInt = Field(alias="orderLineRowCount", ge=0, le=400_000)
    order_line_ticket_quantity: StrictInt = Field(alias="orderLineTicketQuantity", ge=0, le=40_000_000)
    actual_ticket_quantity: StrictInt = Field(alias="actualTicketQuantity", ge=1, le=40_000_000)

    @model_validator(mode="after")
    def validate_account_quantity(self):
        if self.analysis_end_at <= self.analysis_start_at or self.analysis_end_at > self.source_as_of:
            raise PydanticCustomError("invalid_quantity_analysis_period", "Quantity analysis period must be ordered and not after sourceAsOf")
        if self.active_source_row_count != self.ticket_unit_row_count + self.order_line_row_count:
            raise PydanticCustomError("quantity_account_row_count_mismatch", "Active source rows must equal TICKET and ORDER_LINE row counts")
        if self.actual_ticket_quantity != self.ticket_unit_row_count + self.order_line_ticket_quantity:
            raise PydanticCustomError("quantity_account_total_mismatch", "Actual ticket quantity must equal TICKET rows plus ORDER_LINE quantity")
        if self.order_line_row_count == 0 and self.order_line_ticket_quantity != 0:
            raise PydanticCustomError("quantity_account_unit_mismatch", "ORDER_LINE quantity requires at least one ORDER_LINE row")
        if self.order_line_row_count > 0 and self.order_line_ticket_quantity < self.order_line_row_count:
            raise PydanticCustomError("quantity_account_unit_mismatch", "Each ORDER_LINE row must represent at least one ticket")
        return self

    @property
    def scope_key(self) -> tuple[str, str, str, str, str, str]:
        return (self.analysis_id, self.tenant_id, self.supplier_namespace, self.event_id,
                self.performance_id, self.ledger_version_id)


def validate_quantity_evidence_bundle(
    manifests: list[Exp3QuantityScopeManifest],
    evidence: list[Exp3AccountQuantityEvidence],
) -> None:
    """Reject a sidecar that is cross-scope, duplicated, or incomplete vs its manifest."""
    if not manifests:
        raise ValueError("quantity_scope_manifest_required")
    bundle_identities = {
        (manifest.analysis_id, manifest.tenant_id, manifest.supplier_namespace, manifest.hmac_purpose,
         manifest.hmac_key_version, manifest.analysis_start_at, manifest.analysis_end_at)
        for manifest in manifests
    }
    if len(bundle_identities) != 1:
        raise ValueError("quantity_bundle_identity_mixed")
    performance_scopes = {(manifest.analysis_id, manifest.event_id, manifest.performance_id)
                          for manifest in manifests}
    if len(performance_scopes) != len(manifests):
        raise ValueError("duplicate_quantity_performance_scope")
    manifest_by_scope = {manifest.scope_key: manifest for manifest in manifests}
    if len(manifest_by_scope) != len(manifests):
        raise ValueError("duplicate_quantity_scope_manifest")
    seen: set[tuple[tuple[str, str, str, str, str, str], str]] = set()
    grouped: dict[tuple[str, str, str, str, str, str], list[Exp3AccountQuantityEvidence]] = {}
    for row in evidence:
        manifest = manifest_by_scope.get(row.scope_key)
        if manifest is None:
            raise ValueError("quantity_evidence_scope_mismatch")
        if (row.analysis_id != manifest.analysis_id or row.hmac_purpose != manifest.hmac_purpose
                or row.hmac_key_version != manifest.hmac_key_version or row.source_version != manifest.source_version
                or row.source_as_of != manifest.source_as_of or row.analysis_start_at != manifest.analysis_start_at
                or row.analysis_end_at != manifest.analysis_end_at):
            raise ValueError("quantity_evidence_provenance_mismatch")
        identity = (row.scope_key, row.account_hmac)
        if identity in seen:
            raise ValueError("duplicate_account_quantity_evidence")
        seen.add(identity)
        grouped.setdefault(row.scope_key, []).append(row)
    for scope_key, manifest in manifest_by_scope.items():
        rows = grouped.get(scope_key, [])
        if (len(rows) != manifest.expected_account_count
                or sum(row.active_source_row_count for row in rows) != manifest.expected_active_source_row_count
                or sum(row.ticket_unit_row_count for row in rows) != manifest.expected_ticket_unit_row_count
                or sum(row.order_line_row_count for row in rows) != manifest.expected_order_line_row_count
                or sum(row.order_line_ticket_quantity for row in rows) != manifest.expected_order_line_ticket_quantity
                or sum(row.actual_ticket_quantity for row in rows) != manifest.expected_actual_ticket_quantity):
            raise ValueError("quantity_scope_coverage_mismatch")
