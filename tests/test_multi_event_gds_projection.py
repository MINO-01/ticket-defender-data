from datetime import datetime, timezone
from uuid import uuid4

from schemas.multi_event import MultiEventAnalysisRequest
from services.graph_service import GraphService


class _Result:
    def __init__(self, records):
        self.records = records

    async def single(self):
        return self.records[0] if self.records else None

    async def consume(self):
        return None

    def __aiter__(self):
        self._iterator = iter(self.records)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration from None


class _Session:
    def __init__(self):
        self.graphs = {}
        self.project_names = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def run(self, query, **parameters):
        if "gds.version()" in query:
            return _Result([{"version": "2.6.6"}])
        if "gds.graph.project(" in query:
            name = parameters["graph_name"]
            edges = parameters["edges"]
            self.project_names.append(name)
            accounts = sorted({edge["account_index"] for edge in edges})
            nodes = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
            self.graphs[name] = accounts
            return _Result([{"nodeCount": len(nodes)}])
        if "gds.louvain.stream" in query:
            indices = self.graphs[parameters["graph_name"]]
            return _Result([{"accountIndex": float(index), "communityId": 17} for index in indices])
        return _Result([{"graphName": parameters.get("graph_name")}])


class _Driver:
    def __init__(self):
        self.opened = _Session()

    def session(self):
        return self.opened


def _run_immediate(coroutine):
    """Drive a coroutine composed only of the synchronous test doubles above."""
    try:
        yielded = coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError(f"fake GDS call unexpectedly yielded {type(yielded).__name__}")


def _request():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    performances = [str(uuid4()), str(uuid4())]
    scopes = [
        {"eventId": f"event-{index}", "performanceId": performance, "ledgerVersionId": str(uuid4()), "sourceBatchId": f"batch-{index}"}
        for index, performance in enumerate(performances)
    ]
    tickets = []
    for index, scope in enumerate(scopes):
        for account in ("a", "b"):
            tickets.append({
                "accountId": account * 64, "reservationNo": ((str(index + 1) * 31) + account) * 2,
                "eventId": scope["eventId"], "performanceId": scope["performanceId"],
                "ledgerVersionId": scope["ledgerVersionId"], "supplierNamespace": "supplier-a",
                "hmacPurpose": "TRACK1_CONNECTION", "hmacKeyVersion": "v1", "sourceVersion": "source-v1",
                "purchasedAt": now.isoformat(), "refundAccountHash": "e" * 64,
            })
    return MultiEventAnalysisRequest.model_validate({
        "analysisId": str(uuid4()), "tenantId": str(uuid4()), "ledgerVersionSetDigest": "d" * 64,
        "analysisAsOf": now.isoformat(), "analysisStartAt": "2025-12-01T00:00:00Z", "analysisEndAt": now.isoformat(),
        "scoreVersion": "score-v2-exp2-multi-performance-recurrence",
        "projectionVersion": "projection-v2.2-exp2-ipwindow-softhub", "ledgerScopes": scopes, "tickets": tickets,
    })


def test_multi_event_projects_each_full_performance_scope_separately():
    service = GraphService.__new__(GraphService)
    driver = _Driver()
    service.driver = driver

    async def analyze():
        return await service.analyze_multi_event_exp2(_request())

    communities, algorithm_version = _run_immediate(analyze())

    assert len(driver.opened.project_names) == 2
    assert len(set(driver.opened.project_names)) == 2
    assert all(len(groups) == 1 and len(groups[0]) == 2 for groups in communities.values())
    assert algorithm_version == "gds-2.6.6"
