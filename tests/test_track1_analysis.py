import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from core.dependencies import get_graph_service, get_vlm_service
from main import app
from schemas.dto import Track1AnalysisRequest, Track1AnalysisResponse, Track1Cluster, Track1ClusterMember
from services.graph_service import GraphAnalysisUnavailable, GraphResultInvalid, GraphService


REQUEST_ID = "2136558c-2326-4430-a46d-0793494e9b49"
PAYMENT_A = "a" * 64
PAYMENT_B = "b" * 64
DEVICE_A = "c" * 64


def ticket(number, **overrides):
    data = {
        "accountId": f"uid-{number}",
        "reservationNo": f"res-{number}",
        "eventId": "event-1",
        "paymentHash": PAYMENT_A,
        "addressHash": None,
        "deviceIdHash": None,
        "ipHash": None,
    }
    data.update(overrides)
    return data


def payload(tickets=None):
    return {"requestId": REQUEST_ID, "tickets": tickets or [ticket(1), ticket(2)]}


class StubService:
    async def analyze_louvain(self, request):
        return Track1AnalysisResponse(
            requestId=request.request_id,
            status="COMPLETED",
            algorithm="LOUVAIN",
            algorithmVersion="gds-2.6.6;projection-v1;score-v1",
            analyzedAt=datetime.now(timezone.utc),
            clusters=[],
        )

    async def load_json_to_graph(self, tickets):
        pass

    async def detect_macro_clusters(self, threshold):
        return []


class FailingService(StubService):
    def __init__(self, error):
        self.error = error

    async def analyze_louvain(self, request):
        raise self.error


class StubVLMService:
    async def parse_ticket_image(self, base64_image, mime_type):
        return {"zone": "A", "row": "1", "seat": "2"}


class Track1ContractTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_graph_service] = lambda: StubService()
        app.dependency_overrides[get_vlm_service] = lambda: StubVLMService()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()

    def test_success_and_existing_routes(self):
        response = self.client.post("/api/v1/clusters/analyze", json=payload())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["requestId"], REQUEST_ID)
        self.assertEqual(body["status"], "COMPLETED")
        self.assertEqual(body["algorithm"], "LOUVAIN")
        self.assertEqual(body["clusters"], [])
        self.assertEqual(set(body), {
            "requestId", "status", "algorithm", "algorithmVersion", "analyzedAt", "clusters"
        })
        self.assertEqual(self.client.post(
            "/api/v1/clusters/macro",
            json={"tickets": [{
                "accountId": "uid-1", "paymentHash": PAYMENT_A,
                "addressHash": PAYMENT_A, "deviceIdHash": DEVICE_A, "ipHash": PAYMENT_A
            }]},
        ).status_code, 200)
        self.assertIn("/api/v1/parse/ticket-image", app.openapi()["paths"])
        vlm_response = self.client.post(
            "/api/v1/parse/ticket-image", json={"image_base64": "dGVzdA=="}
        )
        self.assertEqual(vlm_response.status_code, 200)
        self.assertEqual(vlm_response.json()["parsed_data"], {
            "zone": "A", "row": "1", "seat": "2"
        })

    def test_invalid_requests_return_422(self):
        cases = [
            {**payload(), "requestId": "not-a-uuid"},
            payload([ticket(1, paymentHash="a" * 63)]),
            payload([ticket(1, paymentHash="A" * 64)]),
            payload([ticket(1, paymentHash=None)]),
            payload([ticket(1), ticket(1)]),
            {"requestId": REQUEST_ID, "tickets": []},
            payload([ticket(1, accountId=" ")]),
            payload([ticket(1, extraField="unsupported")]),
        ]
        for case in cases:
            with self.subTest(case=case):
                response = self.client.post("/api/v1/clusters/analyze", json=case)
                self.assertEqual(response.status_code, 422)

    def test_partial_null_tokens_are_accepted(self):
        response = self.client.post(
            "/api/v1/clusters/analyze",
            json=payload([ticket(1, paymentHash=None, deviceIdHash=DEVICE_A)]),
        )
        self.assertEqual(response.status_code, 200)

    def test_request_id_text_is_preserved(self):
        request_id = REQUEST_ID.upper()
        response = self.client.post(
            "/api/v1/clusters/analyze", json={**payload(), "requestId": request_id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requestId"], request_id)

    def test_malformed_json_returns_422(self):
        response = self.client.post(
            "/api/v1/clusters/analyze", content="{", headers={"content-type": "application/json"}
        )
        self.assertEqual(response.status_code, 422)

    def test_validation_response_does_not_echo_input(self):
        response = self.client.post(
            "/api/v1/clusters/analyze", json=payload([ticket(1, paymentHash="private-value")])
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-value", response.text)
        self.assertEqual(response.json()["detail"], [{"code": "INVALID_CONNECTION_TOKEN"}])

    def test_analysis_errors_are_not_successful_results(self):
        for error, status in (
            (GraphAnalysisUnavailable(), 503),
            (GraphResultInvalid(), 500),
            (RuntimeError("private-value"), 500),
        ):
            with self.subTest(error=type(error).__name__):
                app.dependency_overrides[get_graph_service] = lambda: FailingService(error)
                response = self.client.post("/api/v1/clusters/analyze", json=payload())
                self.assertEqual(response.status_code, status)
                self.assertNotIn("COMPLETED", response.text)
                self.assertNotIn("private-value", response.text)

    def test_cluster_serialization_matches_spring_fields(self):
        response = Track1AnalysisResponse(
            requestId=REQUEST_ID, status="COMPLETED", algorithm="LOUVAIN",
            algorithmVersion="gds-2.6.6;projection-v1;score-v1",
            analyzedAt=datetime.now(timezone.utc),
            clusters=[Track1Cluster(
                clusterId="cluster-1",
                members=[
                    Track1ClusterMember(accountId="uid-1", reservationNo="res-1", eventId="event-1"),
                    Track1ClusterMember(accountId="uid-2", reservationNo="res-2", eventId="event-1"),
                ],
                paymentHashes=[PAYMENT_A], addressHashes=[], deviceIdHashes=[], ipHashes=[],
                riskScore=0.3875,
            )],
        )
        body = response.model_dump(mode="json", by_alias=True)
        self.assertEqual(body["requestId"], REQUEST_ID)
        self.assertEqual(set(body["clusters"][0]), {
            "clusterId", "members", "paymentHashes", "addressHashes", "deviceIdHashes",
            "ipHashes", "riskScore",
        })
        self.assertEqual(body["clusters"][0]["members"][0], {
            "accountId": "uid-1", "reservationNo": "res-1", "eventId": "event-1"
        })


class FakeResult:
    def __init__(self, record=None, rows=None):
        self.record = record
        self.rows = rows or []

    async def single(self):
        return self.record

    async def data(self):
        return self.rows

    async def consume(self):
        return None


class FakeSession:
    def __init__(self, rows=None, fail_louvain=False):
        self.rows = rows or []
        self.fail_louvain = fail_louvain
        self.queries = []
        self.edges = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def run(self, query, **parameters):
        self.queries.append(query)
        if "RETURN gds.version()" in query:
            return FakeResult(record={"version": "2.6.6"})
        if "gds.graph.project(" in query:
            self.edges = parameters["edges"]
            nodes = {edge["source"] for edge in self.edges}
            nodes.update(edge["target"] for edge in self.edges)
            return FakeResult(record={"nodeCount": len(nodes)})
        if "gds.louvain.stream" in query:
            if self.fail_louvain:
                raise RuntimeError("synthetic GDS failure")
            return FakeResult(rows=self.rows)
        if "gds.graph.drop" in query:
            return FakeResult()
        raise AssertionError("Unexpected query")


class FakeDriver:
    def __init__(self, session):
        self.test_session = session

    def session(self):
        return self.test_session


class GraphServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self, session):
        service = GraphService.__new__(GraphService)
        service.driver = FakeDriver(session)
        return service

    async def test_louvain_mapping_and_request_isolation(self):
        request = Track1AnalysisRequest.model_validate(payload([
            ticket(1), ticket(2),
            ticket(3, paymentHash=PAYMENT_B), ticket(4, paymentHash=PAYMENT_B),
        ]))
        session = FakeSession(rows=[
            {"ticketIndex": 0, "communityId": 10},
            {"ticketIndex": 1, "communityId": 10},
            {"ticketIndex": 2, "communityId": 20},
            {"ticketIndex": 3, "communityId": 20},
            {"ticketIndex": -1, "communityId": 10},
            {"ticketIndex": -1, "communityId": 20},
        ])
        response = await self.service(session).analyze_louvain(request)
        self.assertEqual(response.request_id, REQUEST_ID)
        self.assertEqual(len(response.clusters), 2)
        self.assertEqual(
            {frozenset(member.reservation_no for member in cluster.members)
             for cluster in response.clusters},
            {frozenset({"res-1", "res-2"}), frozenset({"res-3", "res-4"})},
        )
        self.assertEqual(
            {tuple(cluster.payment_hashes) for cluster in response.clusters},
            {(PAYMENT_A,), (PAYMENT_B,)},
        )
        self.assertTrue(all(0 <= cluster.risk_score <= 1 for cluster in response.clusters))
        self.assertTrue(any("gds.louvain.stream" in query for query in session.queries))
        self.assertTrue(any("gds.graph.drop" in query for query in session.queries))

    async def test_signal_types_do_not_share_token_nodes(self):
        request = Track1AnalysisRequest.model_validate(payload([
            ticket(1, paymentHash=PAYMENT_A),
            ticket(2, paymentHash=None, ipHash=PAYMENT_A),
        ]))
        edges = GraphService._louvain_edges(request)
        self.assertNotEqual(edges[0]["target"], edges[1]["target"])

    async def test_different_events_do_not_share_token_nodes(self):
        request = Track1AnalysisRequest.model_validate(payload([
            ticket(1, eventId="event-1"),
            ticket(2, eventId="event-2"),
        ]))
        edges = GraphService._louvain_edges(request)
        self.assertNotEqual(edges[0]["target"], edges[1]["target"])

    async def test_invalid_louvain_members_are_rejected(self):
        request = Track1AnalysisRequest.model_validate(payload())
        for rows in (
            [{"ticketIndex": 0, "communityId": 1}],
            [{"ticketIndex": 0, "communityId": 1}, {"ticketIndex": 2, "communityId": 1}],
        ):
            with self.subTest(rows=rows):
                session = FakeSession(rows=rows)
                with self.assertRaises(GraphResultInvalid):
                    await self.service(session).analyze_louvain(request)
                self.assertTrue(any("gds.graph.drop" in query for query in session.queries))

    async def test_gds_numeric_node_property_is_mapped_exactly(self):
        request = Track1AnalysisRequest.model_validate(payload())
        session = FakeSession(rows=[
            {"ticketIndex": 0.0, "communityId": 1},
            {"ticketIndex": 1.0, "communityId": 1},
            {"ticketIndex": -1.0, "communityId": 1},
        ])
        result = await self.service(session).analyze_louvain(request)
        self.assertEqual(len(result.clusters), 1)

        session = FakeSession(rows=[
            {"ticketIndex": 0.5, "communityId": 1},
            {"ticketIndex": 1.0, "communityId": 1},
        ])
        with self.assertRaises(GraphResultInvalid):
            await self.service(session).analyze_louvain(request)

    async def test_cross_event_community_is_rejected(self):
        request = Track1AnalysisRequest.model_validate(payload([
            ticket(1, eventId="event-1"), ticket(2, eventId="event-2")
        ]))
        session = FakeSession(rows=[
            {"ticketIndex": 0, "communityId": 1},
            {"ticketIndex": 1, "communityId": 1},
        ])
        with self.assertRaises(GraphResultInvalid):
            await self.service(session).analyze_louvain(request)

    async def test_algorithm_failure_does_not_return_completed(self):
        request = Track1AnalysisRequest.model_validate(payload())
        session = FakeSession(fail_louvain=True)
        with self.assertRaises(RuntimeError):
            await self.service(session).analyze_louvain(request)
        self.assertTrue(any("gds.graph.drop" in query for query in session.queries))


if __name__ == "__main__":
    unittest.main()
