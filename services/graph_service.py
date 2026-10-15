import logging
import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import List, Dict, Any
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError
from schemas.dto import (
    MacroClusterData, Track1AnalysisRequest, Track1AnalysisResponse,
    Track1Cluster, Track1ClusterMember,
)

logger = logging.getLogger(__name__)

SIGNAL_WEIGHTS = {
    "payment_hash": 0.35,
    "device_id_hash": 0.25,
    "address_hash": 0.15,
    "ip_hash": 0.10,
}


class GraphAnalysisUnavailable(Exception):
    pass


class GraphResultInvalid(Exception):
    pass

class GraphService:
    """
    Neo4j 데이터베이스와의 비동기 통신을 담당하는 서비스 클래스입니다.
    대용량 티켓 데이터 적재 및 GDS 조직망 탐지 알고리즘을 수행합니다.
    """
    def __init__(self, uri: str, user: str, password: str) -> None:
        """GraphService 객체를 초기화하고 Neo4j 비동기 드라이버 커넥션 풀을 생성합니다."""
        self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def close(self) -> None:
        """Neo4j 비동기 드라이버 커넥션을 안전하게 종료하고 자원을 반환합니다."""
        await self.driver.close()

    async def analyze_louvain(self, request: Track1AnalysisRequest) -> Track1AnalysisResponse:
        graph_name = f"track1_{uuid.uuid4().hex}"
        edges = self._louvain_edges(request)
        projection_attempted = False
        failure = None

        async with self.driver.session() as session:
            try:
                try:
                    version_result = await session.run("RETURN gds.version() AS version")
                    version_record = await version_result.single()
                except Neo4jError:
                    raise GraphAnalysisUnavailable("GDS is unavailable") from None
                if not version_record or not version_record["version"]:
                    raise GraphAnalysisUnavailable("GDS version is unavailable")

                projection_attempted = True
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
                clusters = self._clusters_from_communities(request, records)
                return Track1AnalysisResponse(
                    requestId=request.request_id,
                    status="COMPLETED",
                    algorithm="LOUVAIN",
                    algorithmVersion=f"gds-{version_record['version']};projection-v1;score-v1",
                    analyzedAt=datetime.now(timezone.utc),
                    clusters=clusters,
                )
            except Exception as exc:
                failure = exc
                raise
            finally:
                if projection_attempted:
                    try:
                        drop_result = await session.run(
                            "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                            graph_name=graph_name,
                        )
                        await drop_result.consume()
                    except Exception:
                        if failure is None:
                            raise
                        logger.error("Track 1 graph cleanup failed")

    @staticmethod
    def _louvain_edges(request: Track1AnalysisRequest) -> list[dict]:
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

    @staticmethod
    def _clusters_from_communities(
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

    async def load_json_to_graph(self, tickets: list) -> None:
        """
        [비동기 청크 적재] Spring Boot 실시간 JSON 데이터를 다차원 노드(IP, Device 추가)로 적재합니다.
        """
        load_query = """
        UNWIND $tickets AS row
        CALL (row) {
            MERGE (acc:Account {id: row.accountId})
            MERGE (pay:Payment {hash: row.paymentHash})
            MERGE (addr:Address {hash: row.addressHash})
            MERGE (dev:Device {hash: row.deviceIdHash})
            MERGE (ip:IP {hash: row.ipHash})

            MERGE (acc)-[:USED_PAYMENT]->(pay)
            MERGE (acc)-[:DELIVERED_TO]->(addr)
            MERGE (acc)-[:USED_DEVICE]->(dev)
            MERGE (acc)-[:USED_IP]->(ip)
        } IN TRANSACTIONS OF 10000 ROWS;
        """
        
        ticket_dicts = [ticket.model_dump(by_alias=True) for ticket in tickets]
        chunk_size = 10000
        
        async with self.driver.session() as session:
            for i in range(0, len(ticket_dicts), chunk_size):
                chunk = ticket_dicts[i : i + chunk_size]
                result = await session.run(load_query, tickets=chunk)
                await result.consume()
                logger.info(f"[GraphService] Async 다차원 청크 적재 완료: {i + len(chunk)} / {len(ticket_dicts)} 건")

    async def detect_macro_clusters(self, threshold: int = 5) -> List[MacroClusterData]:
        """
        [GDS 알고리즘] Neo4j WCC(Weakly Connected Components)를 활용한 거대 조직망 색출
        결제수단, 기기, IP 중 하나라도 연결된 계정들을 거대한 하나의 클러스터로 묶어냅니다.
        """
        graph_name = f"macro_network_{uuid.uuid4().hex}"
        
        drop_query = f"CALL gds.graph.drop('{graph_name}', false) YIELD graphName;"
        
        project_query = f"""
        CALL gds.graph.project(
            '{graph_name}',
            ['Account', 'Payment', 'Device', 'IP'],
            ['USED_PAYMENT', 'USED_DEVICE', 'USED_IP']
        ) YIELD graphName;
        """
        
        wcc_query = f"""
        CALL gds.wcc.stream('{graph_name}')
        YIELD nodeId, componentId
        WITH gds.util.asNode(nodeId) AS n, componentId
        WHERE 'Account' IN labels(n)
        WITH componentId, collect(n.id) AS accounts, count(n) AS account_count
        WHERE account_count >= $threshold
        RETURN componentId AS cluster_id, account_count, accounts
        ORDER BY account_count DESC
        LIMIT 20;
        """
        
        async with self.driver.session() as session:
            try:
                drop_res = await session.run(drop_query)
                await drop_res.consume()
                
                proj_res = await session.run(project_query)
                await proj_res.consume()
                
                result = await session.run(wcc_query, threshold=threshold)
                records = await result.data()
                return [MacroClusterData(**record) for record in records]
            except Exception as e:
                logger.error(f"[GraphService] GDS 분석 중 오류 발생: {e}")
                raise
            finally:
                final_drop_res = await session.run(drop_query)
                await final_drop_res.consume()
