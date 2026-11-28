# exp3 actual quantity evidence v1

This contract is separate from the exp2 multi-event request. It does not enable an exp3 score. The schema and Backend DTO define the versioned boundary; the artifact producer/consumer wiring remains a prerequisite for a later, explicitly enabled exp3 analysis path.

## Scope and row model

The NDJSON bundle uses two record types:

- `EXP3_QUANTITY_SCOPE_MANIFEST`: one record for each fixed tenant, supplier, event, performance, ledger version, analysis run, source version, and analysis time range.
- `EXP3_ACCOUNT_QUANTITY_EVIDENCE`: one record per account HMAC with at least one effective ACTIVE ticket in that fixed scope and time range.

The records contain no account reference, reservation reference, ticket reference, or original CSV value. `accountHmac` is the existing pseudonymous account identifier. Scope manifests bind tenant, supplier, event, performance, ledger version, HMAC purpose/key version, source version/as-of, and `[analysisStartAt, analysisEndAt)`.

## Quantity interpretation

Counts and totals are calculated after selecting the latest ledger revision for each ticket reference and within the fixed analysis time range:

- each `TICKET` row contributes one to `ticketUnitRowCount` and one ticket to `actualTicketQuantity`;
- each `ORDER_LINE` row contributes one to `orderLineRowCount` and its validated `ticket_quantity` to `orderLineTicketQuantity`;
- cancelled and refunded rows do not appear in account evidence and do not contribute to any active total;
- `activeSourceRowCount = ticketUnitRowCount + orderLineRowCount`;
- `actualTicketQuantity = ticketUnitRowCount + orderLineTicketQuantity`.

The scope manifest states the expected active account count, source row counts, and totals. A bundle contains one analysis, tenant, supplier, HMAC purpose/key version, and analysis time range; each event/performance appears once. Data must reject duplicate account-scope evidence, scope or provenance mismatches, cross-tenant/supplier bundles, and any bundle whose account rows do not add up to the manifest. The completeness flags also carry the ledger full-snapshot result, cancellation handling, duplicate handling, and the deterministic latest-revision rule. An incomplete source is not made complete by matching counts.

## Policy proof and Q

`confirmedPurchaseLimit` is included only when the existing policy snapshot is verified for the same tenant, supplier, event, performance, ledger version, and source version. A confirmed policy requires its snapshot ID and version. `perAccountLimit` is not an actual purchase quantity.

The later exp3 scorer may compute Q only from complete account/performance evidence with a confirmed matching policy. If a required active pair, source quantity, completeness proof, or policy limit is missing or conflicts, use the preregistered neutral `Q=0.5`; do not average only the known subset. Cancellation-only accounts are not inserted as zero-quantity rows. A high Q without an actual shared E2 resource does not create a candidate edge.

## Example

```json
{"recordType":"EXP3_QUANTITY_SCOPE_MANIFEST","contractVersion":"exp3-quantity-evidence-v1","analysisId":"2136558c-2326-4430-a46d-0793494e9b49","tenantId":"2136558c-2326-4430-a46d-0793494e9b50","supplierNamespace":"supplier-demo","eventId":"event-demo","performanceId":"2136558c-2326-4430-a46d-0793494e9b51","ledgerVersionId":"2136558c-2326-4430-a46d-0793494e9b52","hmacPurpose":"TRACK1_CONNECTION","hmacKeyVersion":"key-demo","sourceVersion":"source-demo","sourceAsOf":"2026-11-28T00:00:00Z","analysisStartAt":"2026-11-01T00:00:00Z","analysisEndAt":"2026-11-28T00:00:00Z","ledgerCompleteSnapshot":true,"quantityDataStatus":"COMPLETE","cancellationHandling":"NETTED","duplicateHandling":"DEDUPLICATED","duplicateSelection":"LATEST_REVISION_PER_TICKET_REFERENCE","duplicateSourceRowsRejected":true,"policyVerificationStatus":"CONFIRMED","policySnapshotId":"2136558c-2326-4430-a46d-0793494e9b53","policyVersion":"policy-demo","policySourceVersion":"source-demo","confirmedPurchaseLimit":4,"expectedAccountCount":1,"expectedActiveSourceRowCount":2,"expectedTicketUnitRowCount":1,"expectedOrderLineRowCount":1,"expectedOrderLineTicketQuantity":3,"expectedActualTicketQuantity":4}
{"recordType":"EXP3_ACCOUNT_QUANTITY_EVIDENCE","contractVersion":"exp3-quantity-evidence-v1","analysisId":"2136558c-2326-4430-a46d-0793494e9b49","tenantId":"2136558c-2326-4430-a46d-0793494e9b50","supplierNamespace":"supplier-demo","eventId":"event-demo","performanceId":"2136558c-2326-4430-a46d-0793494e9b51","ledgerVersionId":"2136558c-2326-4430-a46d-0793494e9b52","hmacPurpose":"TRACK1_CONNECTION","hmacKeyVersion":"key-demo","accountHmac":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sourceVersion":"source-demo","sourceAsOf":"2026-11-28T00:00:00Z","analysisStartAt":"2026-11-01T00:00:00Z","analysisEndAt":"2026-11-28T00:00:00Z","activeSourceRowCount":2,"ticketUnitRowCount":1,"orderLineRowCount":1,"orderLineTicketQuantity":3,"actualTicketQuantity":4}
```

The values are synthetic. The record is illustrative and is not a statement that the current exp2 endpoint accepts this format.

## HIGH_QUANTITY

The approved product text is “계정당 최대 수량을 반복해서 샀습니다” and the specified corroborating evidence is an overlapping Track2 resale listing. The current specification does not define how many maximum-quantity purchases count as repeated, the observation window, whether repeat purchases must be on distinct performances, or when a listing overlap is strong enough to display. This contract therefore supplies evidence fields but does not define or emit a `HIGH_QUANTITY` finding. Do not substitute Q, the purchase limit itself, or an unavailable listing as that finding.

## Activation boundary

The current exp2 request, result, quantity calculation, and 11 regression oracles remain unchanged. This contract must be carried only by an explicitly versioned exp3 input path and remain disabled until Gate B is approved and the later exp3 implementation selects it. This initial contract commit validates the schema and bundle totals; it does not yet add the NDJSON upload route, Backend row aggregation, or score consumption.
