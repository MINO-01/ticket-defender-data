import asyncio
import logging
import os
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
    shared_signal_observations,
)
from services.track1_bulk_jobs import GdsResourceExhausted, JobCancelled
from services.track1_scoring_exp2 import build_account_token_graph_exp2, PROJECTION_VERSION_EXP2, SCORE_VERSION_EXP2

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
        observations = shared_signal_observations(request)
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
                        sharedSignalObservations=observations,
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
                    sharedSignalObservations=observations,
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
            sharedSignalObservations=analysis.shared_signal_observations,
            purchaseLimitEvaluations=purchase_limit_evaluations(request),
        )

    async def analyze_multi_event_exp2(self, request):
        """Run one complete, isolated exp2 weighted Louvain projection per performance."""
        tickets_by_scope = {}
        for ticket in request.tickets:
            tickets_by_scope.setdefault(ticket.scope_key, []).append(ticket)

        async with self.driver.session() as session:
            try:
                version_record = await (await session.run("RETURN gds.version() AS version")).single()
            except Neo4jError:
                raise GraphAnalysisUnavailable("GDS is unavailable") from None
            if not version_record or not version_record["version"]:
                raise GraphAnalysisUnavailable("GDS version is unavailable")
            # Keep the GDS runtime separate from both the edge projection score and
            # the final multi-event recurrence score. Those are separate contract fields.
            algorithm_version = f"gds-{version_record['version']}"
            communities = {}
            for scope in request.ledger_scopes:
                tickets = tickets_by_scope.get(scope.key, [])
                graph = build_account_token_graph_exp2(tickets)
                scope_communities = []
                if graph.edges:
                    graph_name = f"track1_mei_{uuid.uuid4().hex}"
                    projection_created = False
                    failure = None
                    try:
                        projection_created = True
                        projected = await session.run(
                            """
                            UNWIND $edges AS edge
                            WITH gds.graph.project(
                                $graph_name, edge.source, edge.target,
                                {sourceNodeProperties:{accountIndex:edge.account_index},
                                 targetNodeProperties:{accountIndex:-1},
                                 relationshipType:'SIGNAL',
                                 relationshipProperties:{weight:edge.weight}},
                                {undirectedRelationshipTypes:['SIGNAL']}
                            ) AS graph
                            RETURN graph.nodeCount AS nodeCount
                            """,
                            graph_name=graph_name, edges=list(graph.edges),
                        )
                        projection_record = await projected.single()
                        expected_nodes = len(graph.accounts) + len(graph.tokens)
                        if not projection_record or projection_record["nodeCount"] != expected_nodes:
                            raise GraphResultInvalid("Incomplete exp2 multi-event graph projection")
                        result = await session.run(
                            """
                            CALL gds.louvain.stream($graph_name, {
                                relationshipWeightProperty:'weight', concurrency:1
                            }) YIELD nodeId, communityId
                            RETURN gds.util.nodeProperty($graph_name,nodeId,'accountIndex') AS accountIndex,
                                   communityId
                            """,
                            graph_name=graph_name,
                        )
                        grouped = {}
                        async for row in result:
                            index = row["accountIndex"]
                            if isinstance(index, int) and 0 <= index < len(graph.accounts):
                                grouped.setdefault(int(row["communityId"]), set()).add(graph.accounts[index].account_id)
                        seen = set().union(*grouped.values()) if grouped else set()
                        if seen != {account.account_id for account in graph.accounts}:
                            raise GraphResultInvalid("Incomplete exp2 multi-event Louvain result")
                        scope_communities = [frozenset(group) for group in grouped.values()]
                    except Exception as exc:
                        failure = exc
                        raise
                    finally:
                        if projection_created:
                            try:
                                await (await session.run(
                                    "CALL gds.graph.drop($graph_name,false) YIELD graphName RETURN graphName",
                                    graph_name=graph_name,
                                )).consume()
                            except Exception:
                                if failure is None:
                                    raise
                                logger.error("Multi-event exp2 graph cleanup failed")
                communities[scope.key] = scope_communities
            return communities, algorithm_version

    async def analyze_multi_event_artifact(self, repository, job_id: str, manifest) -> str:
        """Run complete per-performance exp2 Louvain projections from SQLite pages.

        The ledger is never reconstructed as a Python ticket list or passed as a
        giant Cypher parameter. SQLite indexes provide bounded account/edge pages;
        each performance still receives one complete GDS graph and one Louvain run.
        """
        try:
            async with self.driver.session() as version_session:
                version_record = await (await version_session.run("RETURN gds.version() AS version")).single()
        except Neo4jError:
            raise GraphAnalysisUnavailable("GDS is unavailable") from None
        if not version_record or not version_record["version"]:
            raise GraphAnalysisUnavailable("GDS version is unavailable")
        algorithm = f"gds-{version_record['version']}"
        projected_total = await asyncio.to_thread(repository.prepare_projected_tokens, job_id)
        if projected_total > 5_000_000:
            raise GdsResourceExhausted("PROJECTED_EDGE_LIMIT_EXCEEDED")
        scopes = await asyncio.to_thread(repository.scope_rows, job_id)
        if len(scopes) != len(manifest.ledger_scopes):
            raise GraphResultInvalid("Incomplete fixed scope index")

        async with self.driver.session() as session:
            for scope in scopes:
                scope_id = scope["scope_id"]
                account_count, edge_count = await asyncio.to_thread(repository.graph_scope_count, job_id, scope_id)
                token_count = await asyncio.to_thread(repository.graph_scope_token_count, job_id, scope_id)
                if not edge_count:
                    await asyncio.to_thread(repository.clear_communities, job_id, scope_id)
                    continue
                if account_count < 2 or token_count < 1 or edge_count > 5_000_000:
                    raise GdsResourceExhausted("PERFORMANCE_GRAPH_LIMIT_EXCEEDED")
                graph_name = f"td_mei_{uuid.uuid4().hex}"
                projection_created = False
                account_rows: list[dict] = []
                edge_rows: list[dict] = []
                inserted_accounts = 0
                inserted_edges = 0
                failure = None

                async def flush_accounts():
                    nonlocal inserted_accounts
                    if not account_rows:
                        return
                    await (await session.run("""
                        UNWIND $rows AS row
                        MERGE (a:TDMEIAccount {jobId:$job_id,scopeId:$scope_id,accountId:row.account_id})
                        """, job_id=job_id, scope_id=scope_id, rows=account_rows)).consume()
                    inserted_accounts += len(account_rows)
                    account_rows.clear()

                async def flush_edges():
                    nonlocal inserted_edges
                    if not edge_rows:
                        return
                    await (await session.run("""
                        UNWIND $rows AS row
                        MATCH (a:TDMEIAccount {jobId:$job_id,scopeId:$scope_id,accountId:row.account_id})
                        MERGE (t:TDMEIToken {jobId:$job_id,scopeId:$scope_id,signal:row.signal,token:row.token})
                        MERGE (a)-[r:TDMEI_SIGNAL {jobId:$job_id,scopeId:$scope_id}]->(t)
                        SET r.weight=row.weight
                        """, job_id=job_id, scope_id=scope_id, rows=edge_rows)).consume()
                    inserted_edges += len(edge_rows)
                    edge_rows.clear()

                try:
                    offset = 0
                    while True:
                        page = await asyncio.to_thread(repository.account_page, job_id, scope_id, offset, 500)
                        if not page:
                            break
                        account_rows.extend(page)
                        await flush_accounts()
                        offset += len(page)
                    if inserted_accounts != account_count:
                        raise GraphResultInvalid("Incomplete multi-event account staging")

                    offset = 0
                    while True:
                        page = await asyncio.to_thread(repository.edge_page, job_id, scope_id, offset, 500)
                        if not page:
                            break
                        edge_rows.extend(page)
                        await flush_edges()
                        offset += len(page)
                    if inserted_edges != edge_count:
                        raise GraphResultInvalid("Incomplete multi-event edge staging")

                    projection_created = True
                    result = await session.run("""
                        MATCH (a:TDMEIAccount {jobId:$job_id,scopeId:$scope_id})
                              -[r:TDMEI_SIGNAL {jobId:$job_id,scopeId:$scope_id}]->
                              (t:TDMEIToken {jobId:$job_id,scopeId:$scope_id})
                        WITH gds.graph.project(
                          $graph_name,a,t,
                          {sourceNodeLabels:labels(a),targetNodeLabels:labels(t),
                           sourceNodeProperties:{accountId:a.accountId},
                           targetNodeProperties:{signal:t.signal,token:t.token},
                           relationshipType:'SIGNAL',
                           relationshipProperties:{weight:r.weight}},
                          {undirectedRelationshipTypes:['SIGNAL']}
                        ) AS graph
                        RETURN graph.nodeCount AS nodeCount
                        """, graph_name=graph_name, job_id=job_id, scope_id=scope_id)
                    projection = await result.single()
                    if not projection or projection["nodeCount"] != account_count + token_count:
                        raise GraphResultInvalid("Incomplete exp2 multi-event graph projection")

                    await asyncio.to_thread(repository.clear_communities, job_id, scope_id)
                    result = await session.run("""
                        CALL gds.louvain.stream($graph_name,{relationshipWeightProperty:'weight',concurrency:1})
                        YIELD nodeId,communityId
                        WITH gds.util.asNode(nodeId) AS n,communityId
                        WHERE n:TDMEIAccount AND n.jobId=$job_id AND n.scopeId=$scope_id
                        RETURN n.accountId AS accountId,communityId
                        """, graph_name=graph_name, job_id=job_id, scope_id=scope_id)
                    buffered: list[tuple[str, int]] = []
                    seen = 0
                    async for record in result:
                        buffered.append((record["accountId"], int(record["communityId"])))
                        seen += 1
                        if len(buffered) >= 500:
                            await asyncio.to_thread(repository.add_community_batch, job_id, scope_id, buffered)
                            buffered.clear()
                    if buffered:
                        await asyncio.to_thread(repository.add_community_batch, job_id, scope_id, buffered)
                    if seen != account_count:
                        raise GraphResultInvalid("Incomplete exp2 multi-event Louvain result")
                except MemoryError as exc:
                    failure = exc
                    raise GdsResourceExhausted("GDS_MEMORY_LIMIT_EXCEEDED") from exc
                except Neo4jError as exc:
                    failure = exc
                    if "memory" in str(exc).lower() or "memory" in (exc.code or "").lower():
                        raise GdsResourceExhausted("GDS_MEMORY_LIMIT_EXCEEDED") from exc
                    raise
                except Exception as exc:
                    failure = exc
                    raise
                finally:
                    if projection_created:
                        try:
                            await (await session.run("CALL gds.graph.drop($graph_name,false) YIELD graphName RETURN graphName", graph_name=graph_name)).consume()
                        except Exception:
                            if failure is None:
                                raise
                            logger.error("Multi-event artifact graph cleanup failed")
                    try:
                        await (await session.run("MATCH (n {jobId:$job_id,scopeId:$scope_id}) WHERE n:TDMEIAccount OR n:TDMEIToken DETACH DELETE n", job_id=job_id, scope_id=scope_id)).consume()
                    except Exception:
                        if failure is None:
                            raise
                        logger.error("Multi-event artifact staging cleanup failed")
        return algorithm

    async def analyze_bulk_sqlite(self, repository, job_id: str, manifest) -> str:
        """Project one complete SQLite-staged job and stream only account communities.

        This intentionally does not use ``load_json_to_graph``: that legacy
        method has global labels and materialises a caller-supplied list.  Every
        temporary Neo4j element below has the immutable job id, and cleanup can
        therefore never delete an existing tenant graph.
        """
        graph_name = f"track1_bulk_{job_id.replace('-', '')}"
        projection_created = False
        graph_rows = []
        edge_rows = []
        account_count = 0
        edge_count = 0
        max_edges = int(os.getenv("TRACK1_BULK_MAX_PROJECTED_EDGES", "5000000"))

        async def flush_accounts(session):
            nonlocal account_count
            if graph_rows:
                await (await session.run("""
                    UNWIND $rows AS row
                    MERGE (a:TD36Account {jobId:$job_id, scope:row.scope, accountId:row.account_id})
                    SET a.accountKey = row.scope + '\u0000' + row.account_id
                    """, job_id=job_id, rows=graph_rows)).consume()
                account_count += len(graph_rows)
                graph_rows.clear()

        async def flush_edges(session):
            nonlocal edge_count
            if edge_rows:
                await (await session.run("""
                    UNWIND $rows AS row
                    MATCH (a:TD36Account {jobId:$job_id, scope:row.scope, accountId:row.accountId})
                    MERGE (t:TD36Token {jobId:$job_id, scope:row.scope, signal:row.signal, token:row.token})
                    MERGE (a)-[r:TD36_SIGNAL {jobId:$job_id}]->(t)
                    SET r.weight=row.weight
                    """, job_id=job_id, rows=edge_rows)).consume()
                edge_count += len(edge_rows)
                if edge_count > max_edges:
                    raise GdsResourceExhausted()
                edge_rows.clear()

        async with self.driver.session() as session:
            try:
                try:
                    version_record = await (await session.run("RETURN gds.version() AS version")).single()
                except Neo4jError:
                    raise GraphAnalysisUnavailable("GDS is unavailable") from None
                if not version_record or not version_record["version"]:
                    raise GraphAnalysisUnavailable("GDS version is unavailable")
                algorithm = f"gds-{version_record['version']};{PROJECTION_VERSION};{SCORE_VERSION}"
                account_offset = 0
                while True:
                    account_page = await asyncio.to_thread(repository.graph_row_page, job_id, account_offset, 500)
                    if not account_page:
                        break
                    for row in account_page:
                        graph_rows.append(row)
                    await flush_accounts(session)
                    account_offset += len(account_page)
                    if await asyncio.to_thread(repository.cancelled, job_id):
                        raise JobCancelled()
                await asyncio.to_thread(repository.prepare_projected_tokens, job_id)
                edge_offset = 0
                while True:
                    edge_page = await asyncio.to_thread(repository.projected_edge_page, job_id, edge_offset, 500)
                    if not edge_page:
                        break
                    edge_rows.extend(edge_page)
                    await flush_edges(session)
                    edge_offset += len(edge_page)
                    if await asyncio.to_thread(repository.cancelled, job_id):
                        raise JobCancelled()
                if edge_count == 0:
                    return algorithm
                projection_created = True
                projection = await session.run("""
                    CALL gds.graph.project.cypher(
                      $graph_name,
                      'MATCH (n {jobId: $job_id}) WHERE n:TD36Account OR n:TD36Token RETURN id(n) AS id',
                      'MATCH (a:TD36Account {jobId: $job_id})-[r:TD36_SIGNAL {jobId: $job_id}]->(t:TD36Token {jobId: $job_id}) RETURN id(a) AS source, id(t) AS target, r.weight AS weight',
                      {relationshipProperties: 'weight', validateRelationships: true, parameters: {job_id: $job_id}}
                    ) YIELD nodeCount RETURN nodeCount
                    """, graph_name=graph_name, job_id=job_id)
                projection_record = await projection.single()
                if not projection_record or projection_record["nodeCount"] < account_count:
                    raise GraphResultInvalid("Incomplete bulk graph projection")
                result = await session.run("""
                    CALL gds.louvain.stream($graph_name, {relationshipWeightProperty:'weight', concurrency:1})
                    YIELD nodeId, communityId
                    WITH gds.util.asNode(nodeId) AS n, communityId
                    WHERE n:TD36Account AND n.jobId=$job_id
                    RETURN n.scope AS scope, n.accountId AS accountId, communityId
                    """, graph_name=graph_name, job_id=job_id)
                repository.clear_communities(job_id)
                buffered, seen = [], 0
                async for record in result:
                    buffered.append((record["scope"], record["accountId"], int(record["communityId"])))
                    seen += 1
                    if len(buffered) >= 500:
                        repository.add_community_batch(job_id, buffered)
                        buffered.clear()
                        if await asyncio.to_thread(repository.cancelled, job_id):
                            raise JobCancelled()
                if buffered:
                    repository.add_community_batch(job_id, buffered)
                if seen != account_count:
                    raise GraphResultInvalid("Incomplete bulk Louvain result")
                return algorithm
            except MemoryError as exc:
                raise GdsResourceExhausted() from exc
            except Neo4jError as exc:
                # GDS uses Neo4j errors for projection/algorithm memory admission.
                if "memory" in str(exc).lower() or "memory" in (exc.code or "").lower():
                    raise GdsResourceExhausted() from exc
                raise
            finally:
                if projection_created:
                    try:
                        await (await session.run("CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName", graph_name=graph_name)).consume()
                    except Exception:
                        logger.error("Track 1 bulk graph cleanup failed")
                # The label/key predicates make this cleanup strictly job-local.
                try:
                    await (await session.run("MATCH (n {jobId:$job_id}) WHERE n:TD36Account OR n:TD36Token DETACH DELETE n", job_id=job_id)).consume()
                except Exception:
                    logger.error("Track 1 bulk Neo4j temporary-data cleanup failed")

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
