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
        "scoreVersion": "score-v2-exp2-multi-performance-recurrence",
        "projectionVersion": "projection-v2.2-exp2-ipwindow-softhub",
        "supplierNamespace": "supplier-a",
        "hmacPurpose": "TRACK1_CONNECTION",
        "hmacKeyVersion": "key-v1",
        "ledgerScopes": scopes,
    }


def _artifact(scopes, rows):
    raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
    return raw, MultiEventArtifactManifest.model_validate(_manifest_payload(scopes, raw, len(rows)))


def _exp3_quantity_sidecar(manifest, verified_limit=False, incomplete_scope_ids=()):
    lines = []
    for scope in manifest.ledger_scopes:
        identity = {
            "analysisId": manifest.analysis_id, "tenantId": manifest.tenant_id,
            "supplierNamespace": manifest.supplier_namespace, "eventId": scope.event_id,
            "performanceId": scope.performance_id, "ledgerVersionId": scope.ledger_version_id,
        }
        lines.append({
            "recordType": "EXP3_QUANTITY_SCOPE_MANIFEST", "contractVersion": "exp3-quantity-evidence-v1",
            **identity, "hmacPurpose": manifest.hmac_purpose, "hmacKeyVersion": manifest.hmac_key_version,
            "sourceVersion": "source-v1", "sourceAsOf": manifest.analysis_as_of.isoformat(),
            "analysisStartAt": manifest.analysis_start_at.isoformat(), "analysisEndAt": manifest.analysis_end_at.isoformat(),
            "ledgerCompleteSnapshot": True,
            "quantityDataStatus": "INCOMPLETE" if scope.event_id in incomplete_scope_ids else "COMPLETE",
            "cancellationHandling": "NETTED",
            "duplicateHandling": "DEDUPLICATED", "duplicateSelection": "LATEST_REVISION_PER_TICKET_REFERENCE",
            "duplicateSourceRowsRejected": True,
            "policyVerificationStatus": "CONFIRMED" if verified_limit else "NOT_CONFIRMED",
            "policySnapshotId": scope.policy_snapshot_id,
            "policyVersion": "policy-v1" if verified_limit else None,
            "policySourceVersion": "source-v1" if verified_limit else None,
            "confirmedPurchaseLimit": 1 if verified_limit else None,
            "expectedAccountCount": 1, "expectedActiveSourceRowCount": 1,
            "expectedTicketUnitRowCount": 1, "expectedOrderLineRowCount": 0,
            "expectedOrderLineTicketQuantity": 0, "expectedActualTicketQuantity": 1,
            "rebookingEvidenceStatus": "INCOMPLETE", "expectedReservationEventCount": 1,
            "expectedCancellationEventCount": 0,
        })
        lines.append({
            "recordType": "EXP3_ACCOUNT_QUANTITY_EVIDENCE", "contractVersion": "exp3-quantity-evidence-v1",
            **identity, "hmacPurpose": manifest.hmac_purpose, "hmacKeyVersion": manifest.hmac_key_version,
            "accountHmac": _hash("a"), "sourceVersion": "source-v1", "sourceAsOf": manifest.analysis_as_of.isoformat(),
            "analysisStartAt": manifest.analysis_start_at.isoformat(), "analysisEndAt": manifest.analysis_end_at.isoformat(),
            "activeSourceRowCount": 1, "ticketUnitRowCount": 1, "orderLineRowCount": 0,
            "orderLineTicketQuantity": 0, "actualTicketQuantity": 1,
        })
        lines.append({
            "recordType": "EXP3_REBOOKING_EVENT", "contractVersion": "exp3-quantity-evidence-v1",
            **identity, "hmacPurpose": manifest.hmac_purpose, "hmacKeyVersion": manifest.hmac_key_version,
            "accountHmac": _hash("a"), "ticketHmac": _hash("b"), "sourceVersion": "source-v1",
            "sourceAsOf": manifest.analysis_as_of.isoformat(),
            "analysisStartAt": manifest.analysis_start_at.isoformat(),
            "analysisEndAt": manifest.analysis_end_at.isoformat(), "eventType": "RESERVATION",
            "eventAt": None, "seatZone": "BALCONY", "seatRow": "R1", "seatNumber": "12",
        })
    raw = b"".join(json.dumps(line, separators=(",", ":")).encode() + b"\n" for line in lines)
    parent = manifest.model_dump(by_alias=True, mode="json")
    parent["inputFormatVersion"] = "multi-event-exp3-evidence-ndjson-v1"
    parent["exp3QuantityEvidenceArtifact"] = {
        "contractVersion": "exp3-quantity-evidence-v1", "artifactId": "opaque-exp3-sidecar",
        "objectKey": "opaque-exp3-sidecar", "objectVersion": "exp3-version-1",
        "sha256": hashlib.sha256(raw).hexdigest(), "recordCount": len(lines),
    }
    return raw, MultiEventArtifactManifest.model_validate(parent)


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
        "scoreVersion": "score-v2-exp2-multi-performance-recurrence",
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
        return row_no + 1, supplier_namespace or manifest.supplier_namespace, key_version or manifest.hmac_key_version, False

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


def test_cancelled_artifact_records_are_separate_from_exp2_rows(tmp_path):
    scopes = _scopes()
    active = _ticket(scopes[0], "a", 1, 1, seatZone="BALCONY", seatRow="R1", seatNumber="12",
                     reservedAt="2025-12-20T09:00:00Z", paidAt="2025-12-20T09:05:00Z",
                     accountCreatedAt="2024-01-03T00:00:00Z")
    cancelled = {
        "recordType": "CANCELLED_TICKET",
        "cancelledTicket": {
            "accountHmac": _hash("f"), "ticketHmac": _hash("e"),
            "eventId": scopes[0]["eventId"], "performanceId": scopes[0]["performanceId"],
            "ledgerVersionId": scopes[0]["ledgerVersionId"],
            "supplierNamespace": "supplier-a", "hmacPurpose": "TRACK1_CONNECTION",
            "hmacKeyVersion": "key-v1", "sourceVersion": "cancel-source-v1",
            "sourceRowOrdinal": 2, "seatZone": "BALCONY", "seatRow": "R1", "seatNumber": "12",
            "cancelledAt": "2025-12-20T10:00:00Z",
        },
    }
    raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in (active, cancelled))
    payload = _manifest_payload(scopes, raw, 2)
    payload["inputFormatVersion"] = "multi-event-exp2-ndjson-v2"
    payload["cancelledTicketRecordCount"] = 1
    manifest = MultiEventArtifactManifest.model_validate(payload)
    repository = MultiEventArtifactJobRepository(tmp_path / "cancelled.sqlite")
    accepted = repository.create("cancelled-separated", manifest)
    assert repository.start(accepted.job_id)
    repository.ingest(accepted.job_id, [raw], manifest)

    db = sqlite3.connect(repository.path)
    try:
        assert db.execute("SELECT count(*) FROM mei_artifact_rows WHERE job_id=?", (accepted.job_id,)).fetchone()[0] == 1
        active_metadata = db.execute("""
            SELECT seat_zone,seat_row,seat_number,reserved_at,paid_at,account_created_at
            FROM mei_artifact_rows WHERE job_id=?
            """, (accepted.job_id,)).fetchone()
        assert db.execute("SELECT count(*) FROM mei_artifact_cancelled_tickets WHERE job_id=?", (accepted.job_id,)).fetchone()[0] == 1
        accounts = {row[0] for row in db.execute("SELECT account_id FROM mei_artifact_accounts WHERE job_id=?", (accepted.job_id,))}
        stored_tokens = {row[0] for row in db.execute("SELECT token FROM mei_artifact_token_owners WHERE job_id=?", (accepted.job_id,))}
    finally:
        db.close()
    assert accounts == {_hash("a")}
    assert _hash("f") not in accounts
    assert _hash("e") not in stored_tokens
    assert tuple(active_metadata) == (
        "BALCONY", "R1", "12", "2025-12-20T09:00:00+00:00", "2025-12-20T09:05:00+00:00",
        "2024-01-03T00:00:00+00:00",
    )


def test_exp3_quantity_and_rebooking_sidecar_is_hash_scope_and_coverage_validated(tmp_path):
    scopes = _scopes()
    raw, exp2_manifest = _artifact(scopes, [_ticket(scopes[0], "a", 1, 1), _ticket(scopes[1], "a", 2, 1)])
    sidecar, manifest = _exp3_quantity_sidecar(exp2_manifest)
    repository = MultiEventArtifactJobRepository(tmp_path / "exp3-sidecar.sqlite")
    accepted = repository.create("exp3-sidecar", manifest)
    assert repository.start(accepted.job_id)
    repository.ingest(accepted.job_id, [raw], manifest)
    repository.ingest_exp3_quantity_evidence(
        accepted.job_id, [sidecar], manifest, manifest.exp3_quantity_evidence_artifact,
    )

    db = sqlite3.connect(repository.path)
    try:
        assert db.execute("SELECT count(*) FROM mei_artifact_exp3_quantity_scopes WHERE job_id=?", (accepted.job_id,)).fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM mei_artifact_exp3_account_quantities WHERE job_id=?", (accepted.job_id,)).fetchone()[0] == 2
        events = db.execute("""
            SELECT event_type,ticket_hmac,event_at,seat_zone,seat_row,seat_number
            FROM mei_artifact_exp3_rebooking_events WHERE job_id=? ORDER BY scope_id
            """, (accepted.job_id,)).fetchall()
    finally:
        db.close()
    assert events == [("RESERVATION", _hash("b"), None, "BALCONY", "R1", "12")] * 2


def test_exp3_complete_rebooking_scope_rejects_reservation_without_reserved_at(tmp_path):
    scopes = _scopes()
    raw, exp2_manifest = _artifact(scopes, [_ticket(scopes[0], "a", 1, 1), _ticket(scopes[1], "a", 2, 1)])
    sidecar, manifest = _exp3_quantity_sidecar(exp2_manifest)
    records = [json.loads(line) for line in sidecar.splitlines()]
    next(item for item in records if item["recordType"] == "EXP3_QUANTITY_SCOPE_MANIFEST")["rebookingEvidenceStatus"] = "COMPLETE"
    sidecar = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in records)
    payload = manifest.model_dump(by_alias=True, mode="json")
    payload["exp3QuantityEvidenceArtifact"]["sha256"] = hashlib.sha256(sidecar).hexdigest()
    manifest = MultiEventArtifactManifest.model_validate(payload)
    repository = MultiEventArtifactJobRepository(tmp_path / "exp3-missing-reserved-at.sqlite")
    accepted = repository.create("exp3-missing-reserved-at", manifest)
    assert repository.start(accepted.job_id)
    repository.ingest(accepted.job_id, [raw], manifest)
    with pytest.raises(InputRejected) as error:
        repository.ingest_exp3_quantity_evidence(
            accepted.job_id, [sidecar], manifest, manifest.exp3_quantity_evidence_artifact,
        )
    assert error.value.code == "EXP3_REBOOKING_COMPLETENESS_MISMATCH"


def test_exp3_quantity_sidecar_rejects_tenant_mismatch(tmp_path):
    scopes = _scopes()
    raw, exp2_manifest = _artifact(scopes, [_ticket(scopes[0], "a", 1, 1), _ticket(scopes[1], "a", 2, 1)])
    sidecar, manifest = _exp3_quantity_sidecar(exp2_manifest)
    records = [json.loads(line) for line in sidecar.splitlines()]
    evidence = next(item for item in records if item["recordType"] == "EXP3_ACCOUNT_QUANTITY_EVIDENCE")
    evidence["tenantId"] = str(uuid4())
    sidecar = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in records)
    payload = manifest.model_dump(by_alias=True, mode="json")
    payload["exp3QuantityEvidenceArtifact"]["sha256"] = hashlib.sha256(sidecar).hexdigest()
    manifest = MultiEventArtifactManifest.model_validate(payload)
    repository = MultiEventArtifactJobRepository(tmp_path / "exp3-mismatch.sqlite")
    accepted = repository.create("exp3-mismatch", manifest)
    assert repository.start(accepted.job_id)
    repository.ingest(accepted.job_id, [raw], manifest)
    with pytest.raises(InputRejected) as error:
        repository.ingest_exp3_quantity_evidence(
            accepted.job_id, [sidecar], manifest, manifest.exp3_quantity_evidence_artifact,
        )
    assert error.value.code == "EXP3_QUANTITY_SCOPE_MANIFEST_REQUIRED"


def test_high_quantity_reason_uses_only_complete_verified_active_quantities(tmp_path):
    scopes = _scopes()
    for scope in scopes:
        scope["policySnapshotId"] = str(uuid4())
    raw, exp2_manifest = _artifact(scopes, [
        _ticket(scopes[0], "a", 1, 1), _ticket(scopes[1], "a", 2, 1),
    ])
    sidecar, manifest = _exp3_quantity_sidecar(exp2_manifest, verified_limit=True)
    repository = MultiEventArtifactJobRepository(tmp_path / "high-quantity.sqlite")
    accepted = repository.create("high-quantity", manifest)
    assert repository.start(accepted.job_id)
    repository.ingest(accepted.job_id, [raw], manifest)
    repository.ingest_exp3_quantity_evidence(
        accepted.job_id, [sidecar], manifest, manifest.exp3_quantity_evidence_artifact,
    )

    account = _hash("a")
    candidate_id = "candidate-high-quantity"
    with sqlite3.connect(repository.path) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""
            INSERT INTO mei_artifact_candidates(
              job_id,candidate_id,priority_band,score,distinct_account_count,
              distinct_event_count,distinct_performance_count,verified_relationship_count,summary_json
            ) VALUES(?,?,'LOW',0.25,2,2,2,2,'{}')
        """, (accepted.job_id, candidate_id))
        db.executemany(
            "INSERT INTO mei_artifact_candidate_members(job_id,candidate_id,account_id) VALUES(?,?,?)",
            [(accepted.job_id, candidate_id, account), (accepted.job_id, candidate_id, _hash("c"))],
        )
        performances = repository._candidate_high_quantity_performances(db, accepted.job_id, candidate_id)
    assert performances[account] == {scope.performance_id for scope in manifest.ledger_scopes}

    incomplete, incomplete_manifest = _exp3_quantity_sidecar(
        exp2_manifest, verified_limit=True, incomplete_scope_ids={manifest.ledger_scopes[0].event_id},
    )
    other = MultiEventArtifactJobRepository(tmp_path / "high-quantity-incomplete.sqlite")
    other_accepted = other.create("high-quantity-incomplete", incomplete_manifest)
    assert other.start(other_accepted.job_id)
    other.ingest(other_accepted.job_id, [raw], incomplete_manifest)
    other.ingest_exp3_quantity_evidence(
        other_accepted.job_id, [incomplete], incomplete_manifest,
        incomplete_manifest.exp3_quantity_evidence_artifact,
    )
    with sqlite3.connect(other.path) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""
            INSERT INTO mei_artifact_candidates(
              job_id,candidate_id,priority_band,score,distinct_account_count,
              distinct_event_count,distinct_performance_count,verified_relationship_count,summary_json
            ) VALUES(?,?,'LOW',0.25,2,2,2,2,'{}')
        """, (other_accepted.job_id, candidate_id))
        db.executemany(
            "INSERT INTO mei_artifact_candidate_members(job_id,candidate_id,account_id) VALUES(?,?,?)",
            [(other_accepted.job_id, candidate_id, account), (other_accepted.job_id, candidate_id, _hash("c"))],
        )
        performances = other._candidate_high_quantity_performances(db, other_accepted.job_id, candidate_id)
    assert performances == {}


def test_async_worker_fetches_and_validates_opt_in_exp3_sidecar_before_analysis(tmp_path, monkeypatch):
    scopes = _scopes()
    main_raw, exp2_manifest = _artifact(scopes, [_ticket(scopes[0], "a", 1, 1), _ticket(scopes[1], "a", 2, 1)])
    sidecar, manifest = _exp3_quantity_sidecar(exp2_manifest)

    class SidecarStore:
        def iter_bytes(self, object_key, version):
            objects = {
                ("opaque-artifact-object", "version-1"): main_raw,
                ("opaque-exp3-sidecar", "exp3-version-1"): sidecar,
            }
            assert (object_key, version) in objects
            return iter([objects[(object_key, version)]])

    repository = MultiEventArtifactJobRepository(tmp_path / "exp3-worker.sqlite")
    service = MultiEventArtifactJobService(repository, SidecarStore(), lambda: _Graph())
    monkeypatch.setattr(artifact_jobs.asyncio, "to_thread", _inline_to_thread)
    accepted = repository.create("exp3-worker", manifest)
    _run_immediate(service._run(accepted.job_id))
    assert repository.status(accepted.job_id).status == "COMPLETED"
    with sqlite3.connect(repository.path) as db:
        assert db.execute("SELECT count(*) FROM mei_artifact_exp3_account_quantities WHERE job_id=?", (accepted.job_id,)).fetchone()[0] == 2


def test_clear_stage_removes_cancelled_tickets_and_candidate_member_reasons(tmp_path):
    scopes = _scopes()
    active = _ticket(scopes[0], "a", 1, 1)
    cancelled = {
        "recordType": "CANCELLED_TICKET",
        "cancelledTicket": {
            "accountHmac": _hash("f"), "ticketHmac": _hash("e"),
            "eventId": scopes[0]["eventId"], "performanceId": scopes[0]["performanceId"],
            "ledgerVersionId": scopes[0]["ledgerVersionId"],
            "supplierNamespace": "supplier-a", "hmacPurpose": "TRACK1_CONNECTION",
            "hmacKeyVersion": "key-v1", "sourceVersion": "cancel-source-v1",
            "sourceRowOrdinal": 2, "cancelledAt": "2025-12-20T10:00:00Z",
        },
    }
    raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in (active, cancelled))
    payload = _manifest_payload(scopes, raw, 2)
    payload["inputFormatVersion"] = "multi-event-exp2-ndjson-v2"
    payload["cancelledTicketRecordCount"] = 1
    manifest = MultiEventArtifactManifest.model_validate(payload)
    repository = MultiEventArtifactJobRepository(tmp_path / "stage-cleanup.sqlite")
    accepted = repository.create("stage-cleanup", manifest)
    assert repository.start(accepted.job_id)
    repository.ingest(accepted.job_id, [raw], manifest)

    with sqlite3.connect(repository.path) as db:
        db.execute(
            "INSERT INTO mei_artifact_candidates VALUES(?,?,?,?,?,?,?,?,?)",
            (accepted.job_id, "candidate-cleanup", "MEDIUM", 0.25, 1, 1, 1, 0, "{}"),
        )
        db.execute(
            "INSERT INTO mei_artifact_candidate_members VALUES(?,?,?)",
            (accepted.job_id, "candidate-cleanup", _hash("a")),
        )
        db.execute(
            "INSERT INTO mei_artifact_candidate_member_reasons VALUES(?,?,?,?)",
            (accepted.job_id, "candidate-cleanup", _hash("a"), "[]"),
        )

    repository.clear_stage(accepted.job_id)

    with sqlite3.connect(repository.path) as db:
        assert db.execute(
            "SELECT count(*) FROM mei_artifact_candidate_member_reasons WHERE job_id=?",
            (accepted.job_id,),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM mei_artifact_cancelled_tickets WHERE job_id=?",
            (accepted.job_id,),
        ).fetchone()[0] == 0


def test_artifact_rows_add_v3_metadata_columns_without_losing_legacy_rows(tmp_path):
    path = tmp_path / "legacy-artifact.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE mei_artifact_rows (
              job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, row_ordinal INTEGER NOT NULL,
              account_id TEXT NOT NULL, reservation_no TEXT NOT NULL,
              PRIMARY KEY(job_id,scope_id,row_ordinal)
            )
            """)
        db.execute("INSERT INTO mei_artifact_rows VALUES(?,?,?,?,?)", ("old-job", 0, 1, _hash("a"), _hash("b")))

    MultiEventArtifactJobRepository(path)

    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(mei_artifact_rows)")}
        old_row = db.execute("SELECT account_id,reservation_no FROM mei_artifact_rows WHERE job_id='old-job'").fetchone()
    assert {"seat_zone", "seat_row", "seat_number", "reserved_at", "paid_at", "account_created_at"} <= columns
    assert old_row == (_hash("a"), _hash("b"))


def test_cancelled_artifact_requires_v2_and_matching_count(tmp_path):
    scopes = _scopes()
    cancelled = {
        "recordType": "CANCELLED_TICKET",
        "cancelledTicket": {
            "accountHmac": _hash("f"), "ticketHmac": _hash("e"),
            "eventId": scopes[0]["eventId"], "performanceId": scopes[0]["performanceId"],
            "ledgerVersionId": scopes[0]["ledgerVersionId"],
            "supplierNamespace": "supplier-a", "hmacPurpose": "TRACK1_CONNECTION",
            "hmacKeyVersion": "key-v1", "sourceVersion": "cancel-source-v1",
            "sourceRowOrdinal": 2, "cancelledAt": "2025-12-20T10:00:00Z",
        },
    }
    rows = [_ticket(scopes[0], "a", 1, 1), cancelled]
    raw, v1_manifest = _artifact(scopes, rows)
    v1_repository = MultiEventArtifactJobRepository(tmp_path / "cancelled-v1.sqlite")
    v1_job = v1_repository.create("cancelled-v1", v1_manifest)
    assert v1_repository.start(v1_job.job_id)
    with pytest.raises(InputRejected) as error:
        v1_repository.ingest(v1_job.job_id, [raw], v1_manifest)
    assert error.value.code == "CANCELLED_TICKET_FORMAT_UNSUPPORTED"

    payload = _manifest_payload(scopes, raw, 2)
    payload["inputFormatVersion"] = "multi-event-exp2-ndjson-v2"
    payload["cancelledTicketRecordCount"] = 0
    mismatch_manifest = MultiEventArtifactManifest.model_validate(payload)
    mismatch_repository = MultiEventArtifactJobRepository(tmp_path / "cancelled-count.sqlite")
    mismatch_job = mismatch_repository.create("cancelled-count", mismatch_manifest)
    assert mismatch_repository.start(mismatch_job.job_id)
    with pytest.raises(InputRejected) as error:
        mismatch_repository.ingest(mismatch_job.job_id, [raw], mismatch_manifest)
    assert error.value.code == "CANCELLED_TICKET_RECORD_COUNT_MISMATCH"


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
    # 점수 구성은 구성원이 실제로 공유한 모든 exp2 신호를 씁니다. 기기는 3명 중 2명만 공유합니다.
    assert three_member_candidate.available_signals == ["device_id_hash", "phone_hash"]
    two_member_candidate = candidate_by_members[2]
    assert two_member_candidate.available_signals == ["device_id_hash", "phone_hash"]
    expected_relations = {3: ["phone_hash", "stable_account_cocluster"], 2: ["device_id_hash"]}

    for candidate in candidates.records:
        members = repository.member_page(accepted.job_id, candidate.candidate_id, None, 10)
        relations = repository.relationship_page(accepted.job_id, candidate.candidate_id, None, 10)
        assert members.total_count == candidate.distinct_account_count
        assert [row.signal_type for row in relations.records] == expected_relations[candidate.distinct_account_count]
        assert relations.total_count == len(expected_relations[candidate.distinct_account_count])
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


# 회차 단위 후보, 같은 계정 반복 동시 군집, 퇴역 점수 버전, JSON 경로와의 패리티 테스트입니다.

PURCHASED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()


def _scopes_for(events):
    internal = {event: str(uuid4()) for event in set(events)}
    return [
        {
            "eventId": event, "sourceEventId": internal[event], "performanceId": str(uuid4()),
            "ledgerVersionId": str(uuid4()), "sourceBatchId": f"batch-{index}",
        }
        for index, event in enumerate(events)
    ]


def _parity_rows(scopes):
    """세 회차(공연 2개)에 걸친 합성 fixture입니다. 토큰은 가짜 64자리 16진수입니다."""
    first, second, third = scopes
    rows = []

    def add(scope, account, **signals):
        ordinal = sum(1 for row in rows if row["performanceId"] == scope["performanceId"]) + 1
        reservation = f"{scope['performanceId']}|{ordinal}"
        row = _ticket(scope, account, 0, ordinal, **{"phoneHash": None, "purchasedAt": PURCHASED_AT, **signals})
        row["reservationNo"] = hashlib.sha256(reservation.encode()).hexdigest()
        rows.append(row)

    for scope in (first, second, third):
        for account in ("a", "b"):
            extra = {"deviceIdHash": _hash("d")} if scope is first else {}
            add(scope, account, refundAccountHash=_hash("e"), **extra)
    for scope in (first, second):
        for account in ("c", "d", "e"):
            extra = {"paymentHash": _hash("8")} if scope is second and account in {"c", "d"} else {}
            add(scope, account, ipHash=_hash("7"), ipNetworkType="CORPORATE_EDU", **extra)
    for account in ("f", "1"):
        add(third, account, phoneHash=_hash("f"))
    return rows


def _run_artifact(tmp_path, monkeypatch, scopes, rows, name):
    raw, manifest = _artifact(scopes, rows)
    repository = MultiEventArtifactJobRepository(tmp_path / f"{name}.sqlite")
    service = MultiEventArtifactJobService(repository, lambda: _Store([raw]), lambda: _Graph())
    monkeypatch.setattr(artifact_jobs.asyncio, "to_thread", _inline_to_thread)
    accepted = repository.create(name, manifest)
    _run_immediate(service._run(accepted.job_id))
    return repository, accepted.job_id, manifest


def test_json_and_artifact_paths_produce_identical_candidates(tmp_path, monkeypatch):
    from services.multi_event_analysis import analyze_multi_event

    scopes = _scopes_for(["event-0", "event-0", "event-1"])
    rows = _parity_rows(scopes)
    repository, job_id, manifest = _run_artifact(tmp_path, monkeypatch, scopes, rows, "parity")
    assert repository.status(job_id).status == "COMPLETED"

    request = MultiEventAnalysisRequest.model_validate({
        **{key: value for key, value in _manifest_payload(scopes, b"", len(rows)).items()
           if key in {"analysisAsOf", "analysisStartAt", "analysisEndAt", "scoreVersion", "projectionVersion"}},
        "analysisId": manifest.analysis_id, "tenantId": manifest.tenant_id,
        "ledgerVersionSetDigest": manifest.ledger_version_set_digest,
        "ledgerScopes": [{key: value for key, value in scope.items() if key != "sourceEventId"} for scope in scopes],
        "tickets": rows,
    })
    # _Graph와 같은 가정: 회차마다 투영된 계정 전체가 하나의 GDS 군집입니다.
    projected = {}
    for scope in request.ledger_scopes:
        scope_id = next(index for index, item in enumerate(manifest.ledger_scopes) if item.key == scope.key)
        projected[scope.key] = [frozenset(row["account_id"] for row in repository.account_page(job_id, scope_id, 0, 500))]
    json_result = analyze_multi_event(request, projected, "gds-test")

    def summarize_json(candidate):
        return {
            "id": candidate.candidate_id,
            "members": tuple(candidate.member_account_ids),
            "summary": candidate.model_dump(mode="json", by_alias=True, exclude={"member_account_ids", "relationships", "member_reasons"}),
            "member_reasons": [item.model_dump(mode="json", by_alias=True) for item in candidate.member_reasons],
            "relationships": sorted(
                (relation.signal_type, tuple(sorted(scope.performance_id for scope in relation.source_scopes)))
                for relation in candidate.relationships
            ),
        }

    def summarize_artifact(summary):
        members = repository.member_page(job_id, summary.candidate_id, None, 500)
        relations = repository.relationship_page(job_id, summary.candidate_id, None, 500)
        return {
            "id": summary.candidate_id,
            "members": tuple(members.records),
            "summary": summary.model_dump(mode="json", by_alias=True),
            "member_reasons": [item.model_dump(mode="json", by_alias=True) for item in repository.member_reason_page(job_id, summary.candidate_id, None, 500).records],
            "relationships": sorted(
                (relation.signal_type, tuple(sorted(scope.performance_id for scope in relation.source_scopes)))
                for relation in relations.records
            ),
        }

    artifact_records = repository.candidate_page(job_id, None, 500).records
    assert [summarize_artifact(item) for item in artifact_records] == [summarize_json(item) for item in json_result.candidates]
    assert {len(item.member_account_ids) for item in json_result.candidates} == {2, 5}
    five = next(item for item in json_result.candidates if len(item.member_account_ids) == 5)
    assert [relation.signal_type for relation in five.relationships] == ["stable_account_cocluster"]
    assert "ip_window:CORPORATE_EDU" in five.available_signals
    two = next(item for item in json_result.candidates if len(item.member_account_ids) == 2)
    assert two.distinct_event_count == 2
    assert two.distinct_performance_count == 3
    assert two.score_breakdown.recurrence == pytest.approx(2 / 3)


def test_manifest_accepts_one_event_with_two_performances_and_rejects_one_performance():
    scopes = _scopes_for(["event-0", "event-0"])
    manifest = MultiEventArtifactManifest.model_validate(_manifest_payload(scopes, b"{}\n", 1))
    assert {scope.event_id for scope in manifest.ledger_scopes} == {"event-0"}

    same_performance = _scopes_for(["event-0", "event-0"])
    same_performance[1]["performanceId"] = same_performance[0]["performanceId"]
    with pytest.raises(ValidationError) as error:
        MultiEventArtifactManifest.model_validate(_manifest_payload(same_performance, b"{}\n", 1))
    assert "multi_event_scope_required" in {item["type"] for item in error.value.errors()}


def test_retired_score_version_is_rejected_for_new_jobs_but_stored_jobs_stay_readable(tmp_path, monkeypatch):
    scopes = _scopes()
    retired = {**_manifest_payload(scopes, b"{}\n", 1), "scoreVersion": "score-v2-exp2-multi-event-recurrence"}
    with pytest.raises(ValidationError) as error:
        MultiEventArtifactManifest.model_validate(retired)
    assert error.value.errors()[0]["type"] == "SCORE_VERSION_RETIRED"

    raw, manifest = _artifact(scopes, [_ticket(scopes[0], "a", 1, 1)])
    repository = MultiEventArtifactJobRepository(tmp_path / "retired.sqlite")
    accepted = repository.create("retired-stored", manifest)
    stored = json.loads(manifest.model_dump_json(by_alias=True))
    stored["scoreVersion"] = "score-v2-exp2-multi-event-recurrence"
    with sqlite3.connect(repository.path) as db:
        db.execute("UPDATE mei_artifact_jobs SET manifest_json=? WHERE job_id=?", (json.dumps(stored), accepted.job_id))
    assert repository.manifest(accepted.job_id).score_version == "score-v2-exp2-multi-event-recurrence"

    service = MultiEventArtifactJobService(repository, lambda: _Store([raw]), lambda: _Graph())
    monkeypatch.setattr(artifact_jobs.asyncio, "to_thread", _inline_to_thread)
    _run_immediate(service._run(accepted.job_id))
    status = repository.status(accepted.job_id)
    assert status.status == "INPUT_REJECTED"
    assert status.failure_code == "SCORE_VERSION_RETIRED"


def test_cocluster_comparison_limit_fails_job_without_partial_results(tmp_path, monkeypatch):
    scopes = _scopes_for(["event-0", "event-0"])
    rows = [
        _ticket(scope, account, index * 10 + ordinal, ordinal, phoneHash=None, purchasedAt=PURCHASED_AT,
                ipHash=_hash("7"), ipNetworkType="CORPORATE_EDU")
        for index, scope in enumerate(scopes) for ordinal, account in enumerate(("a", "b", "c"), start=1)
    ]
    monkeypatch.setattr(artifact_jobs, "MAX_RECURRENCE_COMPARISONS", 2)
    repository, job_id, _manifest = _run_artifact(tmp_path, monkeypatch, scopes, rows, "cocluster-limit")
    status = repository.status(job_id)
    assert status.status == "FAILED"
    assert status.failure_code == "RECURRENCE_COMPARISON_LIMIT_EXCEEDED"
    with pytest.raises(HTTPException) as error:
        repository.candidate_page(job_id, None, 10)
    assert error.value.status_code == 409


def test_artifact_ip_window_only_cocluster_candidate_and_dormitory_counterexample(tmp_path, monkeypatch):
    scopes = _scopes_for(["event-0", "event-1"])
    stable = [
        _ticket(scope, account, index * 10 + ordinal, ordinal, phoneHash=None, purchasedAt=PURCHASED_AT,
                ipHash=_hash("7"), ipNetworkType="CORPORATE_EDU")
        for index, scope in enumerate(scopes) for ordinal, account in enumerate(("a", "b", "c"), start=1)
    ]
    repository, job_id, _manifest = _run_artifact(tmp_path, monkeypatch, scopes, stable, "stable-ip")
    records = repository.candidate_page(job_id, None, 10).records
    assert len(records) == 1
    assert records[0].available_signals == ["ip_window:CORPORATE_EDU"]
    assert records[0].score_breakdown.score == pytest.approx(0.1514, abs=1e-4)
    relations = repository.relationship_page(job_id, records[0].candidate_id, None, 10).records
    assert [relation.signal_type for relation in relations] == ["stable_account_cocluster"]
    member_reasons = repository.member_reason_page(job_id, records[0].candidate_id, None, 10).records
    for member in member_reasons:
        recurrence = next(reason for reason in member.reasons if reason.code == "REPEATED_PERFORMANCES")
        assert recurrence.peer_count == 0
        assert set(recurrence.performances) == {scope["performanceId"] for scope in scopes}

    dorm_scopes = _scopes_for(["event-0", "event-1"])
    dorm = [
        _ticket(scope, account, index * 10 + ordinal, ordinal, phoneHash=None, purchasedAt=PURCHASED_AT,
                ipHash=_hash("7"), ipNetworkType="CORPORATE_EDU")
        for index, (scope, accounts) in enumerate(zip(dorm_scopes, (("a", "b", "c"), ("c", "d", "e"))))
        for ordinal, account in enumerate(accounts, start=1)
    ]
    repository, job_id, _manifest = _run_artifact(tmp_path, monkeypatch, dorm_scopes, dorm, "dorm-ip")
    assert repository.status(job_id).status == "COMPLETED"
    assert repository.candidate_page(job_id, None, 10).total_count == 0
