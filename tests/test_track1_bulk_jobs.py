import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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


def row(number, account=None, payment=PAY, performance=PERFORMANCE, event=EVENT):
    return {
        "accountId": account or f"account-{number}", "reservationNo": f"reservation-{number}",
        "eventId": event, "performanceId": performance, "purchasedAt": "2026-11-02T10:00:00Z",
        "effectiveTicketQuantity": 1, "paymentHash": payment,
        "sourceVersion": "source-1", "sourceAsOf": "2026-11-08T10:00:00Z",
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
        repository.prepare_projected_tokens(job_id)
        accounts = repository.graph_row_page(job_id, 0, 10_000)
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
        accounts = self.repo.graph_row_page(accepted.job_id, 0, 100)
        self.assertEqual({(a["scope"], a["account_id"]) for a in accounts}, {(PERFORMANCE.lower(), "same"), (PERFORMANCE.lower(), "other")})
        self.repo.prepare_projected_tokens(accepted.job_id)
        edges = self.repo.projected_edge_page(accepted.job_id, 0, 100)
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

    def test_rejects_source_provenance_mismatch(self):
        data = ndjson([{**row(1), "sourceVersion": "unverified"}])
        accepted = self.create(data, "idempotency-source")
        with self.assertRaises(InputRejected) as raised:
            self.repo.ingest(accepted.job_id, [data], self.repo.manifest(accepted.job_id))
        self.assertEqual(raised.exception.code, "INPUT_SOURCE_MISMATCH")
        from schemas.dto import Track1BulkArtifactManifest
        bad_manifest = manifest(ndjson([row(2)]))
        bad_manifest["performanceScopes"][0]["quantitySourceVersion"] = "other-source"
        with self.assertRaises(Exception) as raised:
            Track1BulkArtifactManifest.model_validate(bad_manifest)
        self.assertIn("MANIFEST_SOURCE_MISMATCH", str(raised.exception))

    def test_result_status_and_all_cluster_members_are_pageable(self):
        data = ndjson([row(1, account="a"), row(2, account="b")])
        accepted = self.create(data, "idempotency-page")
        self.repo.ingest(accepted.job_id, [data], self.repo.manifest(accepted.job_id))
        self.repo.prepare_projected_tokens(accepted.job_id)
        accounts = self.repo.graph_row_page(accepted.job_id, 0, 100)
        self.repo.add_community_batch(accepted.job_id, [(item["scope"], item["account_id"], 1) for item in accounts])
        self.repo.build_result_indexes(accepted.job_id, self.repo.manifest(accepted.job_id))
        self.repo.transition(accepted.job_id, "COMPLETED", algorithm="gds-test;score-v2-exp1")
        results = self.repo.results(accepted.job_id, None, 1, 1)
        self.assertEqual(results.shared_signal_observations_status, "NOT_IMPLEMENTED")
        self.assertEqual(results.purchase_limit_evaluations_status, "NOT_IMPLEMENTED")
        self.assertTrue(results.clusters[0].has_more_members)
        first = self.repo.member_page(accepted.job_id, results.clusters[0].cluster_id, None, 1)
        second = self.repo.member_page(accepted.job_id, results.clusters[0].cluster_id, first.next_cursor, 1)
        self.assertIsNotNone(first.next_cursor)
        self.assertIsNone(second.next_cursor)
        self.assertEqual({member.account_id for member in first.members + second.members}, {"a", "b"})

    def test_retry_purges_committed_derived_rows_before_reingest(self):
        from schemas.dto import Track1BulkArtifactManifest

        partial = ndjson([row(index, account=f"old-{index}") for index in range(500)] + [
            {**row(501, account="invalid"), "sourceVersion": "wrong-source"},
        ])
        accepted = self.create(partial, "retry-derived-data")
        with self.assertRaises(InputRejected):
            self.repo.ingest(accepted.job_id, [partial], self.repo.manifest(accepted.job_id))
        self.repo.transition(accepted.job_id, "FAILED", failure="INPUT_SOURCE_MISMATCH")
        self.repo.retry(accepted.job_id)
        database = sqlite3.connect(self.repo.path)
        try:
            for table in (
                "bulk_input_rows", "bulk_accounts", "bulk_token_owners", "bulk_communities", "bulk_clusters", "bulk_aux_results",
            ):
                self.assertEqual(database.execute(f"SELECT count(*) FROM {table} WHERE job_id=?", (accepted.job_id,)).fetchone()[0], 0)
        finally:
            database.close()

        clean = ndjson([row(900, account="new-a"), row(901, account="new-b")])
        clean_manifest = Track1BulkArtifactManifest.model_validate(manifest(clean))
        self.repo.ingest(accepted.job_id, [clean], clean_manifest)
        accounts = self.repo.graph_row_page(accepted.job_id, 0, 1_000)
        self.assertEqual({item["account_id"] for item in accounts}, {"new-a", "new-b"})
        self.repo.prepare_projected_tokens(accepted.job_id)
        self.assertEqual({edge["accountId"] for edge in self.repo.projected_edge_page(accepted.job_id, 0, 100)}, {"new-a", "new-b"})

    def test_retry_rejects_running_job_without_deleting_its_data(self):
        data = ndjson([row(1)])
        accepted = self.create(data, "retry-running")
        self.repo.ingest(accepted.job_id, [data], self.repo.manifest(accepted.job_id))
        self.repo.transition(accepted.job_id, "RUNNING")
        with self.assertRaises(Exception) as raised:
            self.repo.retry(accepted.job_id)
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        self.assertEqual(self.repo.status(accepted.job_id).status, "RUNNING")
        self.assertEqual(len(self.repo.graph_row_page(accepted.job_id, 0, 10)), 1)

    def test_concurrent_retries_have_one_state_transition_winner(self):
        accepted = self.create(ndjson([row(1)]), "retry-race")
        self.repo.transition(accepted.job_id, "FAILED", failure="SYNTHETIC")

        def retry_once(_):
            try:
                return self.repo.retry(accepted.job_id).status
            except Exception as error:
                return getattr(error, "status_code", None)

        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(retry_once, range(4)))
        self.assertEqual(outcomes.count("QUEUED"), 1)
        self.assertEqual(outcomes.count(409), 3)

    def test_failed_manifest_registration_reuses_job_and_concurrent_requests_converge(self):
        data = ndjson([row(1)])
        first = self.create(data, "failed-first")
        self.repo.transition(first.job_id, "FAILED", failure="SYNTHETIC")
        reused = self.create(data, "failed-second")
        self.assertEqual(reused.job_id, first.job_id)
        self.assertTrue(reused.existing)

        concurrent_data = ndjson([row(2)])
        from schemas.dto import Track1BulkArtifactManifest
        concurrent_manifest = Track1BulkArtifactManifest.model_validate(manifest(concurrent_data))
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(
                lambda index: self.repo.create(f"concurrent-{index}", concurrent_manifest), range(6),
            ))
        self.assertEqual(len({result.job_id for result in results}), 1)

    def test_unconfirmed_and_performance_specific_policy_do_not_block_graph_input(self):
        from schemas.dto import Track1BulkArtifactManifest

        second_performance = "33333333-3333-4333-8333-333333333333"
        data = ndjson([row(1), row(2, performance=second_performance)])
        item = manifest(data, policyVersion="policy-bundle-v1")
        item["performanceScopes"] = [
            {
                **item["performanceScopes"][0],
                "policyVerificationStatus": "POLICY_NOT_CONFIRMED", "policy": None,
                "quantityDataStatus": "UNKNOWN", "quantitySourceVersion": "source-1",
            },
            {
                **item["performanceScopes"][0], "performanceId": second_performance,
                "policy": {"maxTicketsPerAccount": 3, "policyVersion": "performance-b-r2", "effectiveFrom": "2026-11-01T00:00:00Z"},
            },
        ]
        manifest_value = Track1BulkArtifactManifest.model_validate(item)
        accepted = self.repo.create("policy-separated", manifest_value)
        self.repo.ingest(accepted.job_id, [data], manifest_value)
        self.assertEqual(self.repo.status(accepted.job_id).processed_record_count, 2)


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
                self.assertEqual(results.shared_signal_observations_status, "NOT_IMPLEMENTED")
                self.assertEqual(results.purchase_limit_evaluations_status, "NOT_IMPLEMENTED")
                page_one = repo.member_page(accepted.job_id, results.clusters[0].cluster_id, None, 1)
                self.assertIsNotNone(page_one.next_cursor)
                page_two = repo.member_page(accepted.job_id, results.clusters[0].cluster_id, page_one.next_cursor, 1)
                self.assertEqual(len(page_one.members) + len(page_two.members), 2)
        asyncio.run(scenario())


class BulkJobHttpContractTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "sandbox Windows event loop cannot create its wakeup socket")
    def test_job_api_auth_idempotency_and_member_route_are_documented(self):
        from fastapi.testclient import TestClient
        from core.dependencies import get_track1_bulk_job_service
        from main import app

        class HttpService:
            def __init__(self, repo):
                self.repository = repo

            def submit(self, request, key):
                return self.repository.create(key, request.manifest)

        with tempfile.TemporaryDirectory() as temporary:
            data = ndjson([row(1)])
            service = HttpService(BulkJobRepository(Path(temporary) / "jobs.sqlite3"))
            app.dependency_overrides[get_track1_bulk_job_service] = lambda: service
            try:
                with patch.dict(os.environ, {"TRACK1_INTERNAL_SHARED_SECRET": KEY}):
                    client = TestClient(app)
                    self.assertEqual(client.post("/api/v2/clusters/jobs", content="{").status_code, 401)
                    response = client.post(
                        "/api/v2/clusters/jobs", json={"manifest": manifest(data)},
                        headers={"X-TD-Track1-Key": KEY, "Idempotency-Key": "bulk-http-1"},
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    duplicate = client.post(
                        "/api/v2/clusters/jobs", json={"manifest": manifest(data)},
                        headers={"X-TD-Track1-Key": KEY, "Idempotency-Key": "bulk-http-1"},
                    )
                    self.assertTrue(duplicate.json()["existing"])
                    self.assertIn("/api/v2/clusters/jobs/{job_id}/results/{cluster_id}/members", app.openapi()["paths"])
                    client.close()
            finally:
                app.dependency_overrides.clear()

if __name__ == "__main__":
    unittest.main()
