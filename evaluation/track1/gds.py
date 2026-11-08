"""Optional B3 adapter for the existing isolated GDS Louvain implementation."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from .models import BaselineOutput, EvaluationRecord


async def _run_existing_louvain(
    records: tuple[EvaluationRecord, ...], uri: str, password: str
) -> BaselineOutput:
    # Keep B0--B2 and fixture tests independent from optional Neo4j/GDS packages.
    # The existing production implementation is imported only for an explicit B3 run.
    from schemas.dto import Track1AnalysisRequest
    from services.graph_service import GraphService

    scores = {record.ticket_id: 0.0 for record in records}
    labels = {record.ticket_id: f"singleton-{record.ticket_id}" for record in records}
    eligible = [record for record in records if record.has_signal]
    if not eligible:
        return BaselineOutput("B3_GDS_LOUVAIN", "COMPLETED", scores, labels, "No records had observable signals.")

    request = Track1AnalysisRequest.model_validate({
        "requestId": str(uuid4()),
        "tickets": [
            {
                "accountId": record.account_id,
                "reservationNo": record.reservation_no,
                "eventId": record.event_id,
                "paymentHash": record.signals.get("payment_hash"),
                "addressHash": record.signals.get("address_hash"),
                "deviceIdHash": record.signals.get("device_id_hash"),
                "ipHash": record.signals.get("ip_hash"),
            }
            for record in eligible
        ],
    })
    service = GraphService(uri, "neo4j", password)
    try:
        response = await service.analyze_louvain(request)
    finally:
        await service.close()

    by_key = {record.key: record.ticket_id for record in records}
    for cluster in response.clusters:
        for member in cluster.members:
            ticket_id = by_key[(member.account_id, member.reservation_no, member.event_id)]
            scores[ticket_id] = max(scores[ticket_id], cluster.risk_score)
            labels[ticket_id] = cluster.cluster_id
    return BaselineOutput(
        name="B3_GDS_LOUVAIN",
        status="COMPLETED",
        scores=scores,
        cluster_labels=labels,
        algorithm_version=response.algorithm_version,
    )


def b3_existing_gds_louvain(
    records: tuple[EvaluationRecord, ...], *, uri: str | None, password: str | None
) -> BaselineOutput:
    """Run only against a caller-provided isolated GDS instance.

    The adapter never starts Docker or contacts a configured application Neo4j.
    Missing isolation credentials are a deliberate skip, not a synthetic result.
    """

    if not uri or not password:
        return BaselineOutput(
            name="B3_GDS_LOUVAIN",
            status="SKIPPED_GDS_ISOLATION",
            scores={record.ticket_id: 0.0 for record in records},
            detail="Set an isolated GDS URI and password to run B3.",
        )
    try:
        return asyncio.run(_run_existing_louvain(records, uri, password))
    except Exception as error:  # Aggregate failure only; no credentials or source values.
        return BaselineOutput(
            name="B3_GDS_LOUVAIN",
            status="FAILED",
            scores={record.ticket_id: 0.0 for record in records},
            detail=f"{type(error).__name__}: GDS Louvain evaluation failed",
        )
