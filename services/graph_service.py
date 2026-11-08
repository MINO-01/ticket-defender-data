import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError
from schemas.dto import (
    MacroClusterData, Track1AnalysisRequest, Track1AnalysisResponse,
    Track1LimitAnalysisRequest, Track1LimitAnalysisResponse,
)
from services.track1_scoring import (
    PROJECTION_VERSION, SCORE_VERSION, GraphResultInvalid,
    build_account_token_graph, clusters_from_account_communities, purchase_limit_evaluations,
)

logger = logging.getLogger(__name__)


class GraphAnalysisUnavailable(Exception):
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
        graph = build_account_token_graph(request.tickets)
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

                algorithm_version = f"gds-{version_record['version']};{PROJECTION_VERSION};{SCORE_VERSION}"
                # GDS 가용성을 확인한 뒤에만 계정 간 관계 없음을 정상 완료로 돌려줍니다.
                if not graph.edges:
                    return Track1AnalysisResponse(
                        requestId=request.request_id,
                        status="COMPLETED",
                        algorithm="LOUVAIN",
                        algorithmVersion=algorithm_version,
                        analyzedAt=datetime.now(timezone.utc),
                        clusters=[],
                    )

                projection_attempted = True
                projection_result = await session.run(
                    """
                    UNWIND $edges AS edge
                    WITH gds.graph.project(
                        $graph_name, edge.source, edge.target,
                        {
                            sourceNodeProperties: {accountIndex: edge.account_index},
                            targetNodeProperties: {accountIndex: -1},
                            relationshipType: 'SIGNAL',
                            relationshipProperties: {weight: edge.weight}
                        },
                        {undirectedRelationshipTypes: ['SIGNAL']}
                    ) AS graph
                    RETURN graph.nodeCount AS nodeCount
                    """,
                    graph_name=graph_name,
                    edges=list(graph.edges),
                )
                projection_record = await projection_result.single()
                if not projection_record or projection_record["nodeCount"] != graph.node_count:
                    raise GraphResultInvalid("Incomplete graph projection")

                result = await session.run(
                    """
                    CALL gds.louvain.stream($graph_name, {
                        relationshipWeightProperty: 'weight', concurrency: 1
                    })
                    YIELD nodeId, communityId
                    RETURN gds.util.nodeProperty($graph_name, nodeId, 'accountIndex') AS accountIndex,
                           communityId
                    """,
                    graph_name=graph_name,
                )
                records = await result.data()
                clusters = clusters_from_account_communities(request, graph, records)
                return Track1AnalysisResponse(
                    requestId=request.request_id,
                    status="COMPLETED",
                    algorithm="LOUVAIN",
                    algorithmVersion=algorithm_version,
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

    async def analyze_louvain_with_limits(self, request: Track1LimitAnalysisRequest) -> Track1LimitAnalysisResponse:
        """관계 분석과 회차별 한도 평가를 함께 수행하되 결과는 별도 배열로 분리합니다."""

        analysis = await self.analyze_louvain(request)
        return Track1LimitAnalysisResponse(
            requestId=analysis.request_id,
            status=analysis.status,
            algorithm=analysis.algorithm,
            algorithmVersion=analysis.algorithm_version,
            analyzedAt=analysis.analyzed_at,
            clusters=analysis.clusters,
            purchaseLimitEvaluations=purchase_limit_evaluations(request),
        )

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
