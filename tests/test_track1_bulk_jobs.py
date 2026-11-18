import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.track1_bulk_jobs import BulkJobRepository, InputRejected, Track1BulkJobService

if os.name == "nt":  # Test runner sandboxes do not always permit Proactor's self-pipe.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


KEY = "t" * 40
EVENT = "event-1"
PERFORMANCE = "22222222-2222-4222-8222-222222222222"
INTERNAL = "11111111-1111-4111-8111-111111111111"
PAY = "a" * 64


def row(number, account=None, payment=PAY):
    return {
        "accountId": account or f"account-{number}", "reservationNo": f"reservation-{number}",
        "eventId": EVENT, "performanceId": PERFORMANCE, "purchasedAt": "2026-11-02T10:00:00Z",
        "effectiveTicketQuantity": 1, "paymentHash": payment,
    }


def ndjson(rows):
    return b"".join(json.dumps(value, separators=(",", ":")).encode() + b"\n" for value in rows)


def manifest(data, **overrides):
    value = {
        "artifactId": "ledger-36", "objectKey": "ledger-36.ndjson", "objectVersion": "version-1",
        "sha256": hashlib.sha256(data).hexdigest(), "recordCount": data.count(b"\n"),
        "inputFormatVersion": "track1-limit-ndjson-v1", "tenantId": "tenant-a", "eventId": EVENT,
        "ledgerVersion": "v1", "ledgerGeneration": "g1", "ledgerRevision": "r1",
        "sourceVersion": "source-1", "sourceAsOf": "2026-11-08T10:00:00Z",
        "hmacPurpose": "ticket-linking", "hmacKeyVersion": "key-v1", "policyVersion": "policy-v1",
        "performanceScopes": [{
            "internalEventId": INTERNAL, "eventId": EVENT, "performanceId": PERFORMANCE,
            "policyVerificationStatus": "CONFIRMED",
            "policy": {"maxTicketsPerAccount": 2, "policyVersion": "policy-v1", "effectiveFrom": "2026-11-01T00:00:00Z"},
            "quantityDataStatus": "COMPLETE", "quantitySourceVersion": "source-1", "quantityAsOf": "2026-11-08T10:00:00Z",
            "cancellationHandling": "NETTED", "duplicateHandling": "DEDUPLICATED",
        }],
    }
    value.update(overrides)
    return value


class FakeStore:
    def __init__(self, data, chunk=31):
        self.data, self.chunk, self.calls = data, chunk, []

    def iter_bytes(self, name, version):
        self.calls.append((name, version))
        for start in range(0, len(self.data), self.chunk):
            yield self.data[start:start + self.chunk]


class FakeGraph:
    async def analyze_bulk_sqlite(self, repository, job_id, _manifest):
        repository.clear_communities(job_id)
        accounts = list(repository.graph_rows(job_id))
        # The test fake mirrors one complete Louvain community; it intentionally
        # reads staged accounts, never the HTTP/input byte stream.
        repository.add_community_batch(job_id, [(r["scope"], r["account_id"], 7) for r in accounts])
        return "gds-fake;projection-v2.1-account-relative-hub;score-v2-exp1"


class BulkJobRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = BulkJobRepository(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def create(self, data, key="idempotency-1"):
        from schemas.dto import Track1BulkArtifactManifest
        return self.repo.create(key, Track1BulkArtifactManifest.model_validate(manifest(data)))

    def test_idempotency_is_persistent_and_conflicts_on_other_manifest(self):
        data = ndjson([row(1)])
        first = self.create(data)
        again = self.create(data)
        self.assertEqual(first.job_id, again.job_id)
        with self.assertRaises(Exception) as raised:
            self.create(ndjson([row(2)]))
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)

    def test_streaming_ingest_rejects_hash_count_scope_and_duplicates(self):
        data = ndjson([row(1), row(2)])
        accepted = self.create(data)
        parsed = self.repo.manifest(accepted.job_id)
        self.repo.ingest(accepted.job_id, (data[index:index + 7] for index in range(0, len(data), 7)), parsed)
        self.assertEqual(self.repo.status(accepted.job_id).input_record_count, 2)
        duplicate = ndjson([row(1), row(1)])
        second = self.create(duplicate, "idempotency-2")
        with self.assertRaises(InputRejected) as raised:
            self.repo.ingest(second.job_id, [duplicate], self.repo.manifest(second.job_id))
        self.assertEqual(raised.exception.code, "INPUT_ROW_DUPLICATE")
        altered = ndjson([row(3)])
        bad = self.create(altered, "idempotency-3")
        broken = self.repo.manifest(bad.job_id).model_copy(update={"sha256": "b" * 64})
        with self.assertRaises(InputRejected) as raised:
            self.repo.ingest(bad.job_id, [altered], broken)
        self.assertEqual(raised.exception.code, "SHA256_MISMATCH")
        out_of_scope = ndjson([{**row(3), "eventId": "other-event"}])
        third = self.create(out_of_scope, "idempotency-4")
        with self.assertRaises(InputRejected) as raised:
            self.repo.ingest(third.job_id, [out_of_scope], self.repo.manifest(third.job_id))
        self.assertEqual(raised.exception.code, "INPUT_SCOPE_MISMATCH")

    def test_chunk_boundary_preserves_same_account_and_shared_token(self):
        data = ndjson([row(1, account="same"), row(2, account="same"), row(3, account="other")])
        accepted = self.create(data)
        self.repo.ingest(accepted.job_id, (data[i:i + 5] for i in range(0, len(data), 5)), self.repo.manifest(accepted.job_id))
        accounts = list(self.repo.graph_rows(accepted.job_id))
        self.assertEqual({(a["scope"], a["account_id"]) for a in accounts}, {(PERFORMANCE.lower(), "same"), (PERFORMANCE.lower(), "other")})
        edges = list(self.repo.projected_edges(accepted.job_id))
        self.assertEqual({edge["accountId"] for edge in edges}, {"same", "other"})

    def test_10001_rows_are_consumed_as_small_chunks(self):
        # Re-generating each NDJSON line avoids constructing a 10k-ticket request list.
        def chunks():
            for index in range(10_001):
                yield json.dumps(row(index), separators=(",", ":")).encode() + b"\n"
        checksum = hashlib.sha256()
        for part in chunks():
            checksum.update(part)
        from schemas.dto import Track1BulkArtifactManifest
        item = manifest(b"", recordCount=10_001, sha256=checksum.hexdigest())
        accepted = self.repo.create("idempotency-large", Track1BulkArtifactManifest.model_validate(item))
        self.repo.ingest(accepted.job_id, chunks(), self.repo.manifest(accepted.job_id))
        self.assertEqual(self.repo.status(accepted.job_id).processed_record_count, 10_001)


class BulkJobServiceTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "sandbox Windows event loop cannot create its wakeup socket")
    def test_completed_job_pages_results_and_keeps_persistent_state(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temporary:
                data = ndjson([row(1, account="a"), row(2, account="b")])
                repo = BulkJobRepository(Path(temporary) / "jobs.sqlite3")
                service = Track1BulkJobService(repo, FakeStore(data), FakeGraph())
                from schemas.dto import Track1BulkJobCreateRequest
                accepted = service.submit(Track1BulkJobCreateRequest.model_validate({"manifest": manifest(data)}), "request-1")
                await service.tasks[accepted.job_id]
                self.assertEqual(repo.status(accepted.job_id).status, "COMPLETED")
                results = repo.results(accepted.job_id, None, 1, 1)
                self.assertEqual(len(results.clusters), 1)
                self.assertTrue(results.clusters[0].has_more_members)
                self.assertEqual(results.clusters[0].member_count, 2)
        asyncio.run(scenario())

if __name__ == "__main__":
    unittest.main()
