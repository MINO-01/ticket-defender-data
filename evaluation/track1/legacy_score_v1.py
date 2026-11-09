"""main 40a3d1b의 티켓 단위 projection-v1, score-v1을 평가 전용으로 보존합니다.

운영 경로에서 호출하지 않습니다. score-v2와 같은 입력으로 비교하기 위한 기준선이므로
결함을 포함한 원래 동작을 그대로 유지하며, 이 모듈의 계산식은 고치지 않습니다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import uuid

from schemas.dto import Track1AnalysisRequest, Track1AnalysisResponse, Track1Cluster, Track1ClusterMember
from services.track1_scoring import GraphResultInvalid


SIGNAL_WEIGHTS = {
    "payment_hash": 0.35,
    "device_id_hash": 0.25,
    "address_hash": 0.15,
    "ip_hash": 0.10,
}


def legacy_edges(request: Track1AnalysisRequest) -> list[dict]:
    token_ids = {}
    edges = []
    for ticket_index, ticket in enumerate(request.tickets):
        for signal_type, weight in SIGNAL_WEIGHTS.items():
            token = getattr(ticket, signal_type)
            if token is None:
                continue
            token_key = (ticket.event_id, signal_type, token)
            if token_key not in token_ids:
                token_ids[token_key] = len(request.tickets) + len(token_ids)
            edges.append({
                "source": ticket_index,
                "target": token_ids[token_key],
                "ticket_index": ticket_index,
                "weight": weight,
            })
    return edges


def legacy_clusters_from_communities(
    request: Track1AnalysisRequest, records: list[dict]
) -> list[Track1Cluster]:
    communities = defaultdict(list)
    seen = set()
    for record in records:
        ticket_index = record.get("ticketIndex")
        community_id = record.get("communityId")
        if type(ticket_index) is float:
            if not math.isfinite(ticket_index) or not ticket_index.is_integer():
                raise GraphResultInvalid("Invalid Louvain member mapping")
            ticket_index = int(ticket_index)
        if type(ticket_index) is not int or type(community_id) is not int:
            raise GraphResultInvalid("Invalid Louvain result")
        if ticket_index == -1:
            continue
        if ticket_index < 0 or ticket_index >= len(request.tickets) or ticket_index in seen:
            raise GraphResultInvalid("Invalid Louvain member mapping")
        seen.add(ticket_index)
        communities[community_id].append(ticket_index)
    if len(seen) != len(request.tickets):
        raise GraphResultInvalid("Incomplete Louvain result")

    request_ip_counts = Counter(
        (ticket.event_id, ticket.ip_hash)
        for ticket in request.tickets if ticket.ip_hash is not None
    )
    clusters = []
    for ticket_indexes in communities.values():
        if len(ticket_indexes) < 2:
            continue
        members = sorted((request.tickets[index] for index in ticket_indexes), key=lambda t: t.key)
        if len({ticket.event_id for ticket in members}) != 1:
            raise GraphResultInvalid("Louvain community crosses event scope")
        shared = {}
        for signal_type in SIGNAL_WEIGHTS:
            counts = Counter(
                token for ticket in members
                if (token := getattr(ticket, signal_type)) is not None
            )
            shared[signal_type] = sorted(token for token, count in counts.items() if count >= 2)
        if not any(shared.values()):
            continue

        member_keys = [ticket.key for ticket in members]
        identity = json.dumps(
            [str(request.request_id), member_keys], ensure_ascii=False, separators=(",", ":")
        )
        cluster_id = "cluster-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        ip_specificity = max(
            (2 / request_ip_counts[(members[0].event_id, token)]
             for token in shared["ip_hash"]), default=0
        )
        size_score = min((len(members) - 1) / 4, 1)
        risk_score = round(
            0.35 * bool(shared["payment_hash"])
            + 0.25 * bool(shared["device_id_hash"])
            + 0.15 * bool(shared["address_hash"])
            + 0.10 * ip_specificity
            + 0.15 * size_score,
            4,
        )
        clusters.append(Track1Cluster(
            clusterId=cluster_id,
            members=[Track1ClusterMember(
                accountId=ticket.account_id,
                reservationNo=ticket.reservation_no,
                eventId=ticket.event_id,
            ) for ticket in members],
            paymentHashes=shared["payment_hash"],
            addressHashes=shared["address_hash"],
            deviceIdHashes=shared["device_id_hash"],
            ipHashes=shared["ip_hash"],
            riskScore=risk_score,
        ))
    return sorted(clusters, key=lambda cluster: cluster.cluster_id)


async def analyze_louvain_v1(driver, request: Track1AnalysisRequest) -> Track1AnalysisResponse:
    """격리 GDS에서 기존 티켓 단위 투영을 실행합니다. 임시 그래프는 항상 정리합니다."""

    graph_name = f"track1_legacy_{uuid.uuid4().hex}"
    edges = legacy_edges(request)
    async with driver.session() as session:
        version_result = await session.run("RETURN gds.version() AS version")
        version_record = await version_result.single()
        projected = False
        try:
            projected = True
            projection_result = await session.run(
                """
                UNWIND $edges AS edge
                WITH gds.graph.project(
                    $graph_name, edge.source, edge.target,
                    {
                        sourceNodeProperties: {ticketIndex: edge.ticket_index},
                        targetNodeProperties: {ticketIndex: -1},
                        relationshipType: 'SIGNAL',
                        relationshipProperties: {weight: edge.weight}
                    },
                    {undirectedRelationshipTypes: ['SIGNAL']}
                ) AS graph
                RETURN graph.nodeCount AS nodeCount
                """,
                graph_name=graph_name,
                edges=edges,
            )
            projection_record = await projection_result.single()
            if not projection_record or projection_record["nodeCount"] < len(request.tickets):
                raise GraphResultInvalid("Incomplete graph projection")
            result = await session.run(
                """
                CALL gds.louvain.stream($graph_name, {
                    relationshipWeightProperty: 'weight', concurrency: 1
                })
                YIELD nodeId, communityId
                RETURN gds.util.nodeProperty($graph_name, nodeId, 'ticketIndex') AS ticketIndex,
                       communityId
                """,
                graph_name=graph_name,
            )
            records = await result.data()
            return Track1AnalysisResponse(
                requestId=request.request_id,
                status="COMPLETED",
                algorithm="LOUVAIN",
                algorithmVersion=f"gds-{version_record['version']};projection-v1;score-v1",
                analyzedAt=datetime.now(timezone.utc),
                clusters=legacy_clusters_from_communities(request, records),
            )
        finally:
            if projected:
                drop_result = await session.run(
                    "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                    graph_name=graph_name,
                )
                await drop_result.consume()
