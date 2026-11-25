import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from schemas.multi_event import (
    MultiEventArtifactManifest,
    MultiEventAnalysisRequest,
    _java_length_prefixed_sha256,
    _java_string_sort_key,
)
from services.multi_event_artifact_jobs import MultiEventArtifactJobRepository, MultiEventArtifactJobService
from services.track1_bulk_jobs import InputRejected, ObjectVersionMismatch
import services.multi_event_artifact_jobs as artifact_jobs


def _hash(value: int | str) -> str:
    if isinstance(value, str):
        return value * 64
    return f"{value:064x}"


def _scopes():
    return [
        {
            "eventId": f"event-{index}",
            "sourceEventId": str(uuid4()),
            "performanceId": str(uuid4()),
            "ledgerVersionId": str(uuid4()),
            "sourceBatchId": f"batch-{index}",
        }
        for index in range(2)
    ]


def _digest(scopes):
    rows = [
        f"{scope['sourceEventId']}|{scope['performanceId']}|{scope['ledgerVersionId']}"
        for scope in sorted(scopes, key=lambda row: (
            _java_string_sort_key(row["sourceEventId"]),
            _java_string_sort_key(row["performanceId"]),
            _java_string_sort_key(row["ledgerVersionId"]),
        ))
    ]
    return _java_length_prefixed_sha256("MEI_LEDGER_VERSION_SET", "\n".join(rows))


def _ticket(scope, account, reservation, ordinal, **signals):
    row = {
        "accountId": _hash(account),
        "reservationNo": _hash(reservation),
        "eventId": scope["eventId"],
        "performanceId": scope["performanceId"],
        "ledgerVersionId": scope["ledgerVersionId"],
        "supplierNamespace": "supplier-a",
        "hmacPurpose": "TRACK1_CONNECTION",
        "hmacKeyVersion": "key-v1",
        "sourceVersion": "source-v1",
        "sourceRowOrdinal": ordinal,
        "phoneHash": _hash("a"),
    }
    row.update(signals)
    return row


def _manifest_payload(scopes, raw, count):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {
        "artifactId": str(uuid4()),
        "objectKey": "opaque-artifact-object",
        "objectVersion": "version-1",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "recordCount": count,
        "inputFormatVersion": "multi-event-exp2-ndjson-v1",
        "analysisId": str(uuid4()),
        "tenantId": str(uuid4()),
        "ledgerVersionSetDigest": _digest(scopes),
        "analysisAsOf": now.isoformat(),
        "analysisStartAt": "2025-12-01T00:00:00Z",
        "analysisEndAt": now.isoformat(),
        "scoreVersion": "score-v2-exp2-multi-event-recurrence",
        "projectionVersion": "projection-v2.2-exp2-ipwindow-softhub",
        "supplierNamespace": "supplier-a",
        "hmacPurpose": "TRACK1_CONNECTION",
        "hmacKeyVersion": "key-v1",
        "ledgerScopes": scopes,
    }


def _artifact(scopes, rows):
    raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
    return raw, MultiEventArtifactManifest.model_validate(_manifest_payload(scopes, raw, len(rows)))


def test_small_json_contract_does_not_require_internal_source_event_id():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scopes = [{
        "eventId": "event-a", "performanceId": str(uuid4()),
        "ledgerVersionId": str(uuid4()), "sourceBatchId": "batch-a",
    }, {
        "eventId": "event-b", "performanceId": str(uuid4()),
        "ledgerVersionId": str(uuid4()), "sourceBatchId": "batch-b",
    }]
    request = MultiEventAnalysisRequest.model_validate({
        "analysisId": str(uuid4()), "tenantId": str(uuid4()),
        "ledgerVersionSetDigest": _hash("d"), "analysisAsOf": now.isoformat(),
        "analysisStartAt": "2025-12-01T00:00:00Z", "analysisEndAt": now.isoformat(),
        "scoreVersion": "score-v2-exp2-multi-event-recurrence",
        "projectionVersion": "projection-v2.2-exp2-ipwindow-softhub",
        "ledgerScopes": scopes,
        "tickets": [{**_ticket(scopes[0], "a", 1, 1)}],
    })
    assert request.ledger_scopes[0].event_id == "event-a"


def test_artifact_digest_is_recomputed_from_internal_sorted_scope_ids():
    scopes = _scopes()
    raw = b"{}\n"
    payload = _manifest_payload(scopes, raw, 1)
    manifest = MultiEventArtifactManifest.model_validate(payload)
    assert manifest.ledger_version_set_digest == _digest(scopes)

    payload["ledgerVersionSetDigest"] = _hash("f")
    with pytest.raises(ValidationError):
        MultiEventArtifactManifest.model_validate(payload)


def test_status_read_does_not_recover_an_active_job(tmp_path):
    scopes = _scopes()
    _raw, manifest = _artifact(scopes, [_ticket(scopes[0], "a", 1, 1)])
    repository = MultiEventArtifactJobRepository(tmp_path / "recovery.sqlite")
    accepted = repository.create("status-does-not-recover", manifest)
    assert repository.start(accepted.job_id) is True

    assert repository.status(accepted.job_id).status == "RUNNING"
    repository.recover_interrupted()
    assert repository.status(accepted.job_id).status == "INTERRUPTED"


def test_service_resumes_durable_queued_jobs_once(tmp_path, monkeypatch):
    scopes = _scopes()
    repository = MultiEventArtifactJobRepository(tmp_path / "resume.sqlite")
    for index in range(2):
        rows = [_ticket(scopes[0], index + 1, index + 1, 1)]
        _raw, manifest = _artifact(scopes, rows)
        repository.create(f"resume-{index}", manifest)
    expected = set(repository.queued_job_ids())
    assert len(expected) == 2

    scheduled = []

    def fake_create_task(coroutine):
        scheduled.append(coroutine)
        return object()

    monkeypatch.setattr(artifact_jobs.asyncio, "create_task", fake_create_task)
    service = MultiEventArtifactJobService(repository, lambda: None, lambda: None)
    service.resume_queued()
    service.resume_queued()
    assert set(service.tasks) == expected
    assert len(scheduled) == 2
    for coroutine in scheduled:
        coroutine.close()


def test_pending_job_cap_rejects_create_and_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_jobs, "MAX_PENDING_JOBS", 1)
    scopes = _scopes()
    repository = MultiEventArtifactJobRepository(tmp_path / "queue-cap.sqlite")
    _raw, first_manifest = _artifact(scopes, [_ticket(scopes[0], "a", 1, 1)])
    _raw, second_manifest = _artifact(scopes, [_ticket(scopes[0], "b", 2, 1)])
    accepted = repository.create("queue-cap-first", first_manifest)
    with pytest.raises(HTTPException) as create_error:
        repository.create("queue-cap-second", second_manifest)
    assert create_error.value.status_code == 429
    assert create_error.value.detail["code"] == "MULTI_EVENT_JOB_QUEUE_FULL"

    assert repository.start(accepted.job_id)
    repository.transition(accepted.job_id, "FAILED", failure="SYNTHETIC_TEST_FAILURE")
    queued = repository.create("queue-cap-second", second_manifest)
    assert queued.status == "QUEUED"
    with pytest.raises(HTTPException) as retry_error:
        repository.retry(accepted.job_id)
    assert retry_error.value.status_code == 429
    assert retry_error.value.detail["code"] == "MULTI_EVENT_JOB_QUEUE_FULL"


def test_artifact_stream_accepts_more_than_legacy_json_limit_with_light_sink(tmp_path, monkeypatch):
    scopes = _scopes()
    raw = b"{}\n" * 50_001
    manifest = MultiEventArtifactManifest.model_validate(_manifest_payload(scopes, raw, 50_001))

    def light_sink(_db, _job_id, line, row_no, manifest, _scopes, supplier_namespace, key_version):
        assert line == b"{}"
        return row_no + 1, supplier_namespace or manifest.supplier_namespace, key_version or manifest.hmac_key_version

    # This contract test exercises SHA/count streaming above the legacy limit
    # without paying for 50,001 Pydantic and indexed SQLite inserts. The worker
    # test below covers real NDJSON validation and row persistence on small data.
    monkeypatch.setattr(MultiEventArtifactJobRepository, "_ingest_line", staticmethod(light_sink))

    repository = MultiEventArtifactJobRepository(tmp_path / "artifact.sqlite")
    accepted = repository.create("large-ingest", manifest)
    assert accepted.status == "QUEUED"
    assert repository.start(accepted.job_id) is True
    repository.ingest(accepted.job_id, (raw[offset:offset + 64 * 1024] for offset in range(0, len(raw), 64 * 1024)), manifest)
    status = repository.status(accepted.job_id)
    assert status.input_record_count == 50_001
    assert status.processed_record_count == 50_001


@pytest.mark.parametrize("failure_code", ["SHA256_MISMATCH", "RECORD_COUNT_MISMATCH"])
def test_artifact_integrity_errors_are_rejected(tmp_path, failure_code):
    scopes = _scopes()
    rows = [_ticket(scopes[0], "a", 1, 1), _ticket(scopes[0], "b", 2, 2)]
    raw, manifest = _artifact(scopes, rows)
    if failure_code == "SHA256_MISMATCH":
        raw = raw.replace(b"a" * 64, b"b" * 64, 1)
    else:
        payload = _manifest_payload(scopes, raw, 3)
        manifest = MultiEventArtifactManifest.model_validate(payload)

    repository = MultiEventArtifactJobRepository(tmp_path / f"{failure_code}.sqlite")
    accepted = repository.create(failure_code, manifest)
    repository.start(accepted.job_id)
    with pytest.raises(InputRejected) as error:
        repository.ingest(accepted.job_id, [raw], manifest)
    assert error.value.code == failure_code


class _Store:
    def __init__(self, chunks):
        self.chunks = chunks

    def iter_bytes(self, object_key, version):
        assert object_key == "opaque-artifact-object"
        assert version == "version-1"
        if isinstance(self.chunks, Exception):
            raise self.chunks
        return iter(self.chunks)


class _Graph:
    async def analyze_multi_event_artifact(self, repository, job_id, manifest):
        repository.prepare_projected_tokens(job_id)
        for scope in repository.scope_rows(job_id):
            scope_id = scope["scope_id"]
            accounts = repository.account_page(job_id, scope_id, 0, 500)
            repository.add_community_batch(job_id, scope_id, [(row["account_id"], 1) for row in accounts])
        return "gds-test"


def _run_immediate(coroutine):
    """Run a coroutine whose mocked async dependencies complete immediately."""
    try:
        yielded = coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError(f"fake worker unexpectedly yielded {type(yielded).__name__}")


async def _inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def test_async_worker_ingests_stores_candidates_and_serves_complete_pages(tmp_path, monkeypatch):
    scopes = _scopes()
    rows = [
        _ticket(scopes[0], "a", 1, 1), _ticket(scopes[0], "b", 2, 2),
        _ticket(scopes[0], "c", 3, 3),
        _ticket(scopes[0], "a", 4, 4, phoneHash=None, deviceIdHash=_hash("d")),
        _ticket(scopes[0], "b", 5, 5, phoneHash=None, deviceIdHash=_hash("d")),
        _ticket(scopes[1], "a", 6, 1), _ticket(scopes[1], "b", 7, 2),
        _ticket(scopes[1], "c", 8, 3),
        _ticket(scopes[1], "a", 9, 4, phoneHash=None, deviceIdHash=_hash("d")),
        _ticket(scopes[1], "b", 10, 5, phoneHash=None, deviceIdHash=_hash("d")),
    ]
    raw, manifest = _artifact(scopes, rows)
    repository = MultiEventArtifactJobRepository(tmp_path / "worker.sqlite")
    service = MultiEventArtifactJobService(repository, lambda: _Store([raw]), lambda: _Graph())
    monkeypatch.setattr(artifact_jobs.asyncio, "to_thread", _inline_to_thread)

    accepted = repository.create("worker-idempotency", manifest)
    assert accepted.status == "QUEUED"
    _run_immediate(service._run(accepted.job_id))
    assert repository.status(accepted.job_id).status == "COMPLETED"
    candidates = repository.candidate_page(accepted.job_id, None, 10)
    assert candidates.total_count == 2
    candidate_by_members = {row.distinct_account_count: row for row in candidates.records}
    assert set(candidate_by_members) == {2, 3}
    three_member_candidate = candidate_by_members[3]
    assert three_member_candidate.available_signals == ["phone_hash"]
    two_member_candidate = candidate_by_members[2]
    assert two_member_candidate.available_signals == ["device_id_hash"]

    for candidate in candidates.records:
        members = repository.member_page(accepted.job_id, candidate.candidate_id, None, 10)
        relations = repository.relationship_page(accepted.job_id, candidate.candidate_id, None, 10)
        assert members.total_count == candidate.distinct_account_count
        assert relations.total_count == 1
        assert relations.records[0].source_scope_count == 2
        assert relations.records[0].account_count == len(members.records)
    duplicate = repository.create("worker-idempotency", manifest)
    assert duplicate.existing is True
    assert duplicate.job_id == accepted.job_id

    with sqlite3.connect(repository.path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM mei_artifact_evidence_groups WHERE typeof(group_id)='text'").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM mei_artifact_evidence_members WHERE typeof(group_id)='text'").fetchone()[0] == 10


def test_object_version_mismatch_is_durable_input_rejection(tmp_path, monkeypatch):
    scopes = _scopes()
    rows = [_ticket(scopes[0], "a", 1, 1)]
    raw, manifest = _artifact(scopes, rows)
    repository = MultiEventArtifactJobRepository(tmp_path / "version.sqlite")
    service = MultiEventArtifactJobService(repository, lambda: _Store(ObjectVersionMismatch()), lambda: _Graph())
    monkeypatch.setattr(artifact_jobs.asyncio, "to_thread", _inline_to_thread)
    accepted = repository.create("version-idempotency", manifest)
    _run_immediate(service._run(accepted.job_id))
    status = repository.status(accepted.job_id)
    assert status.status == "INPUT_REJECTED"
    assert status.failure_code == "OBJECT_VERSION_MISMATCH"
