import asyncio
import os
import socket
import unittest
from uuid import uuid4

from neo4j.exceptions import Neo4jError, ServiceUnavailable

from schemas.dto import Track1AnalysisRequest
from services.graph_service import GraphService


def ticket(account, reservation, event, signal, token):
    return {
        "accountId": account,
        "reservationNo": reservation,
        "eventId": event,
        signal: token * 64,
    }


def request(tickets):
    return Track1AnalysisRequest.model_validate({"requestId": str(uuid4()), "tickets": tickets})


class TrackedSession:
    def __init__(self, delegate, driver):
        self.delegate = delegate
        self.driver = driver

    async def __aenter__(self):
        await self.delegate.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.delegate.__aexit__(*args)

    async def run(self, query, **parameters):
        if "gds.graph.project(" in query:
            self.driver.projected.append(parameters["graph_name"])
        if "gds.louvain.stream" in query and self.driver.fail_louvain:
            query = query.replace(
                "relationshipWeightProperty: 'weight'",
                "relationshipWeightProperty: 'missing'",
            )
        if "gds.graph.drop" in query:
            self.driver.dropped.append(parameters["graph_name"])
            if self.driver.fail_drop:
                raise RuntimeError("Synthetic graph cleanup failure")
        return await self.delegate.run(query, **parameters)


class TrackedDriver:
    def __init__(self, delegate):
        self.delegate = delegate
        self.projected = []
        self.dropped = []
        self.fail_louvain = False
        self.fail_drop = False

    def session(self):
        return TrackedSession(self.delegate.session(), self)

    async def close(self):
        await self.delegate.close()


@unittest.skipUnless(
    os.getenv("TD_GDS_TEST_URI") and os.getenv("TD_GDS_TEST_PASSWORD"),
    "isolated GDS test environment is not configured",
)
class Track1GdsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = GraphService(
            os.environ["TD_GDS_TEST_URI"], "neo4j", os.environ["TD_GDS_TEST_PASSWORD"]
        )
        self.service.driver = TrackedDriver(self.service.driver)

    async def asyncTearDown(self):
        await self.service.close()

    async def query(self, statement, **parameters):
        async with self.service.driver.delegate.session() as session:
            result = await session.run(statement, **parameters)
            return await result.data()

    async def graph_names(self):
        return {
            record["graphName"]
            for record in await self.query("CALL gds.graph.list() YIELD graphName RETURN graphName")
        }

    async def store_counts(self):
        nodes = await self.query("MATCH (n) RETURN count(n) AS count")
        relationships = await self.query("MATCH ()-[r]->() RETURN count(r) AS count")
        return nodes[0]["count"], relationships[0]["count"]

    async def test_versions_and_gds_operations(self):
        dbms = await self.query("CALL dbms.components() YIELD versions RETURN versions")
        gds = await self.query("RETURN gds.version() AS version")
        self.assertIn("5.12.0", dbms[0]["versions"])
        self.assertEqual(gds[0]["version"], "2.6.6")

        procedures = await self.query(
            "SHOW PROCEDURES YIELD name WHERE name IN "
            "['gds.louvain.stream', 'gds.graph.drop'] RETURN name"
        )
        self.assertEqual(
            {record["name"] for record in procedures},
            {"gds.louvain.stream", "gds.graph.drop"},
        )
        functions = await self.query(
            "SHOW FUNCTIONS YIELD name WHERE name = 'gds.graph.project' RETURN name"
        )
        self.assertEqual([record["name"] for record in functions], ["gds.graph.project"])

    async def test_real_louvain_partition_and_no_persistent_writes(self):
        before_graphs = await self.graph_names()
        before_store = await self.store_counts()
        tickets = [
            ticket("shared-account", "pay-1", "event-1", "paymentHash", "a"),
            ticket("shared-account", "pay-2", "event-1", "paymentHash", "a"),
            ticket("dev-1", "dev-1", "event-1", "deviceIdHash", "b"),
            ticket("dev-2", "dev-2", "event-1", "deviceIdHash", "b"),
            ticket("addr-1", "addr-1", "event-1", "addressHash", "c"),
            ticket("addr-2", "addr-2", "event-1", "addressHash", "c"),
            ticket("ip-1", "ip-1", "event-1", "ipHash", "d"),
            ticket("ip-2", "ip-2", "event-1", "ipHash", "d"),
            ticket("other-event", "event-2-1", "event-2", "paymentHash", "a"),
            ticket("isolated", "isolated-1", "event-1", "deviceIdHash", "e"),
        ]
        analysis = await self.service.analyze_louvain(request(tickets))

        self.assertEqual(analysis.algorithm, "LOUVAIN")
        self.assertIn("gds-2.6.6", analysis.algorithm_version)
        self.assertEqual(
            {frozenset(member.reservation_no for member in cluster.members)
             for cluster in analysis.clusters},
            {
                frozenset({"pay-1", "pay-2"}),
                frozenset({"dev-1", "dev-2"}),
                frozenset({"addr-1", "addr-2"}),
                frozenset({"ip-1", "ip-2"}),
            },
        )
        submitted = {
            (item["accountId"], item["reservationNo"], item["eventId"])
            for item in tickets
        }
        returned = {
            (member.account_id, member.reservation_no, member.event_id)
            for cluster in analysis.clusters for member in cluster.members
        }
        self.assertTrue(returned <= submitted)
        self.assertTrue(all(0 <= cluster.risk_score <= 1 for cluster in analysis.clusters))
        self.assertEqual(await self.graph_names(), before_graphs)
        self.assertEqual(await self.store_counts(), before_store)
        self.assertEqual(self.service.driver.projected, self.service.driver.dropped)

    async def test_concurrent_requests_are_isolated_and_cleaned(self):
        before_graphs = await self.graph_names()
        first = request([
            ticket("a1", "a1", "event-1", "paymentHash", "a"),
            ticket("a2", "a2", "event-1", "paymentHash", "a"),
        ])
        second = request([
            ticket("b1", "b1", "event-1", "deviceIdHash", "b"),
            ticket("b2", "b2", "event-1", "deviceIdHash", "b"),
        ])
        results = await asyncio.gather(
            self.service.analyze_louvain(first), self.service.analyze_louvain(second)
        )
        self.assertEqual(
            [{member.reservation_no for cluster in result.clusters for member in cluster.members}
             for result in results],
            [{"a1", "a2"}, {"b1", "b2"}],
        )
        self.assertEqual(len(set(self.service.driver.projected)), 2)
        self.assertEqual(set(self.service.driver.projected), set(self.service.driver.dropped))
        self.assertEqual(await self.graph_names(), before_graphs)

    async def test_louvain_failure_cleans_its_graph(self):
        before_graphs = await self.graph_names()
        self.service.driver.fail_louvain = True
        analysis_request = request([
            ticket("a1", "a1", "event-1", "paymentHash", "a"),
            ticket("a2", "a2", "event-1", "paymentHash", "a"),
        ])
        with self.assertRaises(Neo4jError):
            await self.service.analyze_louvain(analysis_request)
        self.assertEqual(len(self.service.driver.projected), 1)
        self.assertEqual(await self.graph_names(), before_graphs)
        self.assertEqual(self.service.driver.projected, self.service.driver.dropped)

    async def test_neo4j_connection_failure_is_not_a_completed_result(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            disconnected = GraphService(
                f"bolt://127.0.0.1:{port}", "neo4j", os.environ["TD_GDS_TEST_PASSWORD"]
            )
            try:
                with self.assertRaises(ServiceUnavailable):
                    await disconnected.analyze_louvain(request([
                        ticket("a1", "a1", "event-1", "paymentHash", "a"),
                        ticket("a2", "a2", "event-1", "paymentHash", "a"),
                    ]))
            finally:
                await disconnected.close()

    async def test_cleanup_failure_is_reported(self):
        before_graphs = await self.graph_names()
        self.service.driver.fail_drop = True
        analysis_request = request([
            ticket("a1", "a1", "event-1", "paymentHash", "a"),
            ticket("a2", "a2", "event-1", "paymentHash", "a"),
        ])
        try:
            with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
                await self.service.analyze_louvain(analysis_request)
            self.assertEqual(len(self.service.driver.projected), 1)
        finally:
            for graph_name in self.service.driver.projected:
                if graph_name not in before_graphs:
                    await self.query(
                        "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                        graph_name=graph_name,
                    )
        self.assertEqual(await self.graph_names(), before_graphs)


if __name__ == "__main__":
    unittest.main()
