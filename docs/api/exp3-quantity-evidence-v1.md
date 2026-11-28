# exp3 actual quantity evidence v1

This contract is a separate, opt-in input sidecar for the existing multi-event artifact job. It does not change the exp2 request rows or scoring. The Backend registers an immutable object reference in `exp3QuantityEvidenceArtifact`; Data streams, hashes, validates and stores the sidecar before the existing exp2 analysis runs. The new input format is `multi-event-exp3-evidence-ndjson-v1` and is disabled by default in Backend.

## Scope and row model

The sidecar NDJSON bundle uses three record types:

- `EXP3_QUANTITY_SCOPE_MANIFEST`: one record for each fixed tenant, supplier, event, performance, ledger version, analysis run, source version, and analysis time range.
- `EXP3_ACCOUNT_QUANTITY_EVIDENCE`: one record per account HMAC with at least one effective ACTIVE ticket in that fixed scope and time range.
- `EXP3_REBOOKING_EVENT`: one pseudonymous reservation, cancellation or refund event for E1. Reservation `eventAt` is copied only from `reservedAt`; when absent it remains null. `purchasedAt` is never substituted.

The records contain no account reference, reservation reference, ticket reference, or original CSV value. `accountHmac` and `ticketHmac` are existing purpose/key-versioned HMAC identifiers. Scope manifests bind tenant, supplier, event, performance, ledger version, HMAC purpose/key version, source version/as-of, and `[analysisStartAt, analysisEndAt)`. `ticketHmac` gives E1 a stable pseudonymous ticket identity for duplicate removal; it is not the reservation HMAC.

The sidecar record ceiling is 1,200,500: up to 500 scope manifests and up to 400,000 each of account quantity, reservation, and cancellation/refund records. Each event category and all active source rows have a 400,000-row ceiling across the fixed analysis bundle.

## Quantity interpretation

Counts and totals are calculated after selecting the latest ledger revision for each ticket reference and within the fixed analysis time range:

- each `TICKET` row contributes one to `ticketUnitRowCount` and one ticket to `actualTicketQuantity`;
- each `ORDER_LINE` row contributes one to `orderLineRowCount` and its validated `ticket_quantity` to `orderLineTicketQuantity`;
- cancelled and refunded rows do not appear in account evidence and do not contribute to any active total;
- `activeSourceRowCount = ticketUnitRowCount + orderLineRowCount`;
- `actualTicketQuantity = ticketUnitRowCount + orderLineTicketQuantity`.

The scope manifest states the expected active account count, source row counts, totals, E1 evidence status, and expected reservation/cancellation event counts. A bundle contains one analysis, tenant, supplier, HMAC purpose/key version, and analysis time range; each event/performance appears once. Data rejects duplicate account-scope evidence, duplicate ticket events, scope or provenance mismatches, cross-tenant/supplier bundles, and any bundle whose rows do not add up to the manifest. The completeness flags also carry the ledger full-snapshot result, cancellation handling, duplicate handling, and the deterministic latest-revision rule. An incomplete source is not made complete by matching counts. A scope with no active rows has zero counts and no account row; it cannot provide Q. If source version or source-as-of is unavailable, the scope is marked incomplete/unknown and Q remains neutral.

## Policy proof and Q

`confirmedPurchaseLimit` is included only when the existing policy snapshot is verified for the same tenant, supplier, event, performance, ledger version, and source version. A confirmed policy requires its snapshot ID and version. `perAccountLimit` is not an actual purchase quantity.

The later exp3 scorer may compute Q only from complete account/performance evidence with a confirmed matching policy. If a required active pair, source quantity, completeness proof, or policy limit is missing or conflicts, use the preregistered neutral `Q=0.5`; do not average only the known subset. Cancellation-only accounts are not inserted as zero-quantity rows. A high Q without an actual shared E2 resource does not create a candidate edge.

The E1 sidecar preserves event inputs for the preregistered 90-second rule. Active quantity rows are selected by `purchasedAt`; E1 reservation rows use `reservedAt` in [analysisStartAt, analysisEndAt). If `reservedAt` is missing, a row is retained only when its `purchasedAt` is in that period, its `eventAt` stays null, and the scope is marked INCOMPLETE. `purchasedAt` is never used as an event time. Cancellation/refund rows use their actual `cancelledAt` in the period. Data does not choose cancellation/reservation pairs during ingestion. It stores the original event direction, event time, seat strings, account HMAC and ticket HMAC so the exp3 scorer can deterministically select one-to-one relations. Cancelled/refunded events are never converted into active quantity.

## Example

```json
{"recordType":"EXP3_QUANTITY_SCOPE_MANIFEST","contractVersion":"exp3-quantity-evidence-v1","analysisId":"2136558c-2326-4430-a46d-0793494e9b49","tenantId":"2136558c-2326-4430-a46d-0793494e9b50","supplierNamespace":"supplier-demo","eventId":"event-demo","performanceId":"2136558c-2326-4430-a46d-0793494e9b51","ledgerVersionId":"2136558c-2326-4430-a46d-0793494e9b52","hmacPurpose":"TRACK1_CONNECTION","hmacKeyVersion":"key-demo","sourceVersion":"source-demo","sourceAsOf":"2026-11-28T00:00:00Z","analysisStartAt":"2026-11-01T00:00:00Z","analysisEndAt":"2026-11-28T00:00:00Z","ledgerCompleteSnapshot":true,"quantityDataStatus":"COMPLETE","cancellationHandling":"NETTED","duplicateHandling":"DEDUPLICATED","duplicateSelection":"LATEST_REVISION_PER_TICKET_REFERENCE","duplicateSourceRowsRejected":true,"policyVerificationStatus":"CONFIRMED","policySnapshotId":"2136558c-2326-4430-a46d-0793494e9b53","policyVersion":"policy-demo","policySourceVersion":"source-demo","confirmedPurchaseLimit":4,"expectedAccountCount":1,"expectedActiveSourceRowCount":2,"expectedTicketUnitRowCount":1,"expectedOrderLineRowCount":1,"expectedOrderLineTicketQuantity":3,"expectedActualTicketQuantity":4}
{"recordType":"EXP3_ACCOUNT_QUANTITY_EVIDENCE","contractVersion":"exp3-quantity-evidence-v1","analysisId":"2136558c-2326-4430-a46d-0793494e9b49","tenantId":"2136558c-2326-4430-a46d-0793494e9b50","supplierNamespace":"supplier-demo","eventId":"event-demo","performanceId":"2136558c-2326-4430-a46d-0793494e9b51","ledgerVersionId":"2136558c-2326-4430-a46d-0793494e9b52","hmacPurpose":"TRACK1_CONNECTION","hmacKeyVersion":"key-demo","accountHmac":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sourceVersion":"source-demo","sourceAsOf":"2026-11-28T00:00:00Z","analysisStartAt":"2026-11-01T00:00:00Z","analysisEndAt":"2026-11-28T00:00:00Z","activeSourceRowCount":2,"ticketUnitRowCount":1,"orderLineRowCount":1,"orderLineTicketQuantity":3,"actualTicketQuantity":4}
```

The values are synthetic. The record is illustrative and is not a statement that the current exp2 endpoint accepts this format.

## HIGH_QUANTITY

The approved product text is “계정당 최대 수량을 반복해서 샀습니다”. On this explicit sidecar path, emit the reason only when the same account reaches a confirmed per-account limit through actual ACTIVE purchases in at least two distinct performances in the selected analysis period. Each counted performance must have a complete ledger, quantity, and limit provenance. Exclude incomplete scope, quantity/limit provenance, and cancelled or refunded rows. This reason does not change the score, band, or rank. Track2 seat overlap is not required; do not display overlap unless it has actually been verified. Q, the configured limit by itself, and missing listings are not proof of this reason.

## Activation boundary

The current exp2 request, result, quantity calculation, and 11 regression oracles remain unchanged. This sidecar is carried only by the explicitly versioned exp3 input path. Data validates and stores it but does not consume it for scoring; exp3 scoring remains disabled until Gate B approval and the PR 4 implementation.
