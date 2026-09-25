import logging
from typing import List, Dict, Any
from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)

class GraphService:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def close(self) -> None:
        await self.driver.close()

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
                await session.run(load_query, tickets=chunk)
                logger.info(f"[GraphService] Async 다차원 청크 적재 완료: {i + len(chunk)} / {len(ticket_dicts)} 건")

    async def detect_macro_clusters(self, threshold: int = 5) -> List[Dict[str, Any]]:
        """
        [GDS 알고리즘] Neo4j WCC(Weakly Connected Components)를 활용한 거대 조직망 색출
        결제수단, 기기, IP 중 하나라도 연결된 계정들을 거대한 하나의 클러스터로 묶어냅니다.
        """
        drop_query = "CALL gds.graph.drop('macro_network', false) YIELD graphName;"
        
        # 2. 다차원 엣지를 포함한 분석용 그래프 메모리 투영
        project_query = """
        CALL gds.graph.project(
            'macro_network',
            ['Account', 'Payment', 'Device', 'IP'],
            ['USED_PAYMENT', 'USED_DEVICE', 'USED_IP']
        ) YIELD graphName;
        """
        
        # 3. WCC 알고리즘 실행 및 군집화된 계정 도출
        wcc_query = """
        CALL gds.wcc.stream('macro_network')
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
                await session.run(drop_query)
                await session.run(project_query)
                
                result = await session.run(wcc_query, threshold=threshold)
                records = await result.data()
                return records
            except Exception as e:
                logger.error(f"[GraphService] GDS 분석 중 오류 발생: {e}")
                raise
            finally:
                # 분석이 끝나면 반드시 램(RAM) 자원 반환
                await session.run(drop_query)