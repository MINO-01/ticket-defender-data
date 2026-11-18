"""Durable, bounded-memory Track 1 bulk analysis jobs.

The job database is deliberately separate from Neo4j.  It holds the immutable
manifest, the streamed NDJSON rows and result indexes, so a Python restart does
not turn an in-flight analysis into a successful empty result.  Neo4j receives
only job-labelled temporary nodes/relationships and they are removed by job id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from fastapi import HTTPException
from schemas.dto import (
    Track1BulkArtifactManifest, Track1BulkClusterSummary, Track1BulkJobAccepted,
    Track1BulkClusterMemberPage, Track1BulkJobResults, Track1BulkJobStatus, Track1LimitTicketInput,
)
from services.track1_scoring import DEFAULT_PARAMETERS, SIGNALS, is_hub, score_v2


STATES = {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "INPUT_REJECTED", "CANCELLED", "INTERRUPTED"}
TERMINAL = {"COMPLETED", "FAILED", "INPUT_REJECTED", "CANCELLED"}
MAX_NDJSON_LINE_BYTES = 1_048_576


class BulkJobError(Exception):
    code = "BULK_ANALYSIS_FAILED"


class InputRejected(BulkJobError):
    code = "INPUT_REJECTED"

    def __init__(self, code: str = "INPUT_REJECTED") -> None:
        self.code = code
        super().__init__(code)


class ObjectVersionMismatch(InputRejected):
    code = "OBJECT_VERSION_MISMATCH"


class JobCancelled(BulkJobError):
    code = "JOB_CANCELLED"


class GdsResourceExhausted(BulkJobError):
    code = "GDS_RESOURCE_EXHAUSTED"


class PrivateObjectStore:
    """Only a configured bucket/prefix can be read; callers never supply a URL."""

    def iter_bytes(self, object_name: str, expected_version: str) -> Iterable[bytes]:
        raise NotImplementedError


class S3PrivateObjectStore(PrivateObjectStore):
    def __init__(self) -> None:
        self.bucket = os.getenv("TRACK1_BULK_S3_BUCKET", "")
        self.prefix = os.getenv("TRACK1_BULK_S3_PREFIX", "track1-ledgers/")
        endpoint = os.getenv("TRACK1_BULK_S3_ENDPOINT")
        if not self.bucket or not self.prefix or endpoint is None:
            raise RuntimeError("TRACK1_BULK_S3 configuration is required")
        try:
            import boto3  # optional at import time so unit tests need no S3 client
        except ImportError as exc:  # pragma: no cover - deployment configuration error
            raise RuntimeError("boto3 is required for Track 1 bulk input") from exc
        self.client = boto3.client("s3", endpoint_url=endpoint)

    def iter_bytes(self, object_name: str, expected_version: str) -> Iterable[bytes]:
        # object_name was schema-validated as an opaque name.  Prefix is server
        # configuration, not an untrusted request value.
        key = self.prefix.rstrip("/") + "/" + object_name
        head = self.client.head_object(Bucket=self.bucket, Key=key, VersionId=expected_version)
        if head.get("VersionId") != expected_version:
            raise ObjectVersionMismatch()
        response = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=expected_version)
        for chunk in response["Body"].iter_chunks(chunk_size=64 * 1024):
            if chunk:
                yield chunk


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class BulkJobRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bulk_jobs (
                  job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                  manifest_json TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL, input_record_count INTEGER NOT NULL DEFAULT 0,
                  processed_record_count INTEGER NOT NULL DEFAULT 0,
                  algorithm_version TEXT, failure_code TEXT,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bulk_input_rows (
                  job_id TEXT NOT NULL, row_no INTEGER NOT NULL, scope TEXT NOT NULL,
                  account_id TEXT NOT NULL, reservation_no TEXT NOT NULL, ticket_reference TEXT,
                  row_json TEXT NOT NULL, PRIMARY KEY(job_id, row_no),
                  UNIQUE(job_id, scope, account_id, reservation_no, ticket_reference),
                  FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bulk_accounts (
                  job_id TEXT NOT NULL, scope TEXT NOT NULL, account_id TEXT NOT NULL,
                  PRIMARY KEY(job_id, scope, account_id),
                  FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bulk_token_owners (
                  job_id TEXT NOT NULL, scope TEXT NOT NULL, signal TEXT NOT NULL,
                  token TEXT NOT NULL, account_id TEXT NOT NULL, projected INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(job_id, scope, signal, token, account_id),
                  FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bulk_communities (
                  job_id TEXT NOT NULL, scope TEXT NOT NULL, account_id TEXT NOT NULL, community_id INTEGER NOT NULL,
                  PRIMARY KEY(job_id, scope, account_id), FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bulk_clusters (
                  job_id TEXT NOT NULL, cluster_id TEXT NOT NULL, community_id INTEGER NOT NULL,
                  scope TEXT NOT NULL, risk_score REAL NOT NULL, member_count INTEGER NOT NULL, signals_json TEXT NOT NULL,
                  PRIMARY KEY(job_id, cluster_id), FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bulk_aux_results (
                  job_id TEXT PRIMARY KEY, observations_json TEXT NOT NULL, limits_json TEXT NOT NULL,
                  FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
            """)

    def recover_interrupted(self) -> None:
        with _connect(self.path) as db:
            db.execute("UPDATE bulk_jobs SET status='INTERRUPTED', failure_code='PROCESS_RESTARTED', updated_at=? WHERE status='RUNNING'", (_now(),))

    def create(self, idempotency_key: str, manifest: Track1BulkArtifactManifest) -> Track1BulkJobAccepted:
        payload = manifest.model_dump(mode="json", by_alias=True)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with _connect(self.path) as db:
            existing = db.execute("SELECT job_id, fingerprint, status FROM bulk_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
                return Track1BulkJobAccepted(jobId=existing["job_id"], status=existing["status"], existing=True)
            duplicate = db.execute("SELECT job_id, status FROM bulk_jobs WHERE fingerprint=? AND status NOT IN ('FAILED','INPUT_REJECTED','CANCELLED')", (fingerprint,)).fetchone()
            if duplicate:
                return Track1BulkJobAccepted(jobId=duplicate["job_id"], status=duplicate["status"], existing=True)
            job_id, now = str(uuid.uuid4()), _now()
            db.execute("INSERT INTO bulk_jobs(job_id,idempotency_key,manifest_json,fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id, idempotency_key, json.dumps(payload, separators=(",", ":")), fingerprint, "QUEUED", now, now))
            return Track1BulkJobAccepted(jobId=job_id, status="QUEUED")

    def status(self, job_id: str) -> Track1BulkJobStatus:
        with _connect(self.path) as db:
            row = db.execute("SELECT * FROM bulk_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
        return Track1BulkJobStatus(jobId=row["job_id"], status=row["status"], inputRecordCount=row["input_record_count"], processedRecordCount=row["processed_record_count"], algorithmVersion=row["algorithm_version"], failureCode=row["failure_code"], createdAt=row["created_at"], updatedAt=row["updated_at"])

    def manifest(self, job_id: str) -> Track1BulkArtifactManifest:
        with _connect(self.path) as db:
            row = db.execute("SELECT manifest_json FROM bulk_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
        return Track1BulkArtifactManifest.model_validate_json(row["manifest_json"])

    def transition(self, job_id: str, status: str, *, failure: str | None = None, algorithm: str | None = None) -> None:
        with _connect(self.path) as db:
            db.execute("UPDATE bulk_jobs SET status=?, failure_code=?, algorithm_version=COALESCE(?, algorithm_version), updated_at=? WHERE job_id=?", (status, failure, algorithm, _now(), job_id))

    def cancel(self, job_id: str) -> Track1BulkJobStatus:
        with _connect(self.path) as db:
            row = db.execute("SELECT status FROM bulk_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
            if row["status"] not in TERMINAL:
                db.execute("UPDATE bulk_jobs SET cancel_requested=1, updated_at=? WHERE job_id=?", (_now(), job_id))
        return self.status(job_id)

    def retry(self, job_id: str) -> Track1BulkJobAccepted:
        state = self.status(job_id)
        if state.status not in {"FAILED", "INPUT_REJECTED", "CANCELLED", "INTERRUPTED"}:
            raise HTTPException(409, detail={"code": "JOB_NOT_RETRYABLE"})
        with _connect(self.path) as db:
            db.execute("DELETE FROM bulk_input_rows WHERE job_id=?", (job_id,))
            db.execute("UPDATE bulk_jobs SET status='QUEUED', failure_code=NULL, cancel_requested=0, input_record_count=0, processed_record_count=0, updated_at=? WHERE job_id=?", (_now(), job_id))
        return Track1BulkJobAccepted(jobId=job_id, status="QUEUED")

    def cancelled(self, job_id: str) -> bool:
        with _connect(self.path) as db:
            row = db.execute("SELECT cancel_requested FROM bulk_jobs WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def ingest(self, job_id: str, chunks: Iterable[bytes], manifest: Track1BulkArtifactManifest) -> None:
        digest, carry, row_no = hashlib.sha256(), b"", 0
        scopes = {scope.performance_id.lower(): scope for scope in manifest.performance_scopes}
        with _connect(self.path) as db:
            for chunk in chunks:
                digest.update(chunk)
                carry += chunk
                if len(carry) > MAX_NDJSON_LINE_BYTES:
                    raise InputRejected("INPUT_ROW_TOO_LARGE")
                lines = carry.split(b"\n")
                carry = lines.pop()
                for line in lines:
                    row_no = self._ingest_line(db, job_id, line.rstrip(b"\r"), row_no, manifest, scopes)
                    if row_no % 500 == 0:
                        db.commit()
                        if self.cancelled(job_id):
                            raise JobCancelled()
            if carry:
                row_no = self._ingest_line(db, job_id, carry.rstrip(b"\r"), row_no, manifest, scopes)
            if digest.hexdigest() != manifest.sha256:
                raise InputRejected("SHA256_MISMATCH")
            if row_no != manifest.record_count:
                raise InputRejected("RECORD_COUNT_MISMATCH")
            db.execute("UPDATE bulk_jobs SET input_record_count=?, processed_record_count=?, updated_at=? WHERE job_id=?", (row_no, row_no, _now(), job_id))

    @staticmethod
    def _ingest_line(db, job_id, line, row_no, manifest, scopes) -> int:
        if not line:
            raise InputRejected("INPUT_ROW_MISSING")
        try:
            ticket = Track1LimitTicketInput.model_validate(json.loads(line))
        except Exception as exc:
            raise InputRejected("INPUT_ROW_INVALID") from exc
        scope = scopes.get(ticket.analysis_scope)
        if scope is None or scope.event_id != ticket.event_id or ticket.event_id != manifest.event_id:
            raise InputRejected("INPUT_SCOPE_MISMATCH")
        if ticket.source_version != manifest.source_version or ticket.source_as_of != manifest.source_as_of:
            raise InputRejected("INPUT_SOURCE_MISMATCH")
        if (
            scope.quantity_source_version != manifest.source_version
            or scope.quantity_as_of != manifest.source_as_of
            or scope.policy_verification_status != "CONFIRMED"
            or scope.policy is None
            or scope.policy.policy_version != manifest.policy_version
        ):
            raise InputRejected("INPUT_POLICY_OR_SCOPE_PROVENANCE_MISMATCH")
        row_no += 1
        try:
            db.execute("INSERT INTO bulk_input_rows VALUES(?,?,?,?,?,?,?)", (job_id, row_no, ticket.analysis_scope, ticket.account_id, ticket.reservation_no, ticket.ticket_reference or "", json.dumps(ticket.model_dump(mode="json", by_alias=True), separators=(",", ":"))))
        except sqlite3.IntegrityError as exc:
            raise InputRejected("INPUT_ROW_DUPLICATE") from exc
        db.execute("INSERT OR IGNORE INTO bulk_accounts VALUES(?,?,?)", (job_id, ticket.analysis_scope, ticket.account_id))
        for signal in SIGNALS:
            token = getattr(ticket, signal)
            if token is not None:
                db.execute("INSERT OR IGNORE INTO bulk_token_owners(job_id,scope,signal,token,account_id) VALUES(?,?,?,?,?)", (job_id, ticket.analysis_scope, signal, token, ticket.account_id))
        return row_no

    def graph_row_page(self, job_id: str, offset: int, limit: int) -> list[dict]:
        with _connect(self.path) as db:
            return [dict(row) for row in db.execute(
                "SELECT scope, account_id FROM bulk_accounts WHERE job_id=? ORDER BY scope, account_id LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            )]

    def prepare_projected_tokens(self, job_id: str) -> None:
        with _connect(self.path) as db:
            counts = {row["scope"]: row["n"] for row in db.execute("SELECT scope,count(*) n FROM bulk_accounts WHERE job_id=? GROUP BY scope", (job_id,))}
            db.execute("UPDATE bulk_token_owners SET projected=0 WHERE job_id=?", (job_id,))
            for token in db.execute("SELECT scope,signal,token,count(*) n FROM bulk_token_owners WHERE job_id=? GROUP BY scope,signal,token", (job_id,)):
                if token["n"] < 2 or is_hub(token["signal"], token["n"], DEFAULT_PARAMETERS, counts[token["scope"]]):
                    continue
                db.execute("UPDATE bulk_token_owners SET projected=1 WHERE job_id=? AND scope=? AND signal=? AND token=?", (job_id, token["scope"], token["signal"], token["token"]))

    def projected_edge_page(self, job_id: str, offset: int, limit: int) -> list[dict]:
        with _connect(self.path) as db:
            rows = db.execute("""SELECT scope,signal,token,account_id FROM bulk_token_owners
                WHERE job_id=? AND projected=1 ORDER BY scope,signal,token,account_id LIMIT ? OFFSET ?""", (job_id, limit, offset))
            return [{"scope": row["scope"], "signal": row["signal"], "token": row["token"], "accountId": row["account_id"], "weight": DEFAULT_PARAMETERS.signal_weights[row["signal"]]} for row in rows]

    def clear_communities(self, job_id: str) -> None:
        with _connect(self.path) as db:
            db.execute("DELETE FROM bulk_communities WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM bulk_clusters WHERE job_id=?", (job_id,))

    def add_community_batch(self, job_id: str, rows: list[tuple[str, str, int]]) -> None:
        with _connect(self.path) as db:
            db.executemany("INSERT INTO bulk_communities VALUES(?,?,?,?)", [(job_id, *row) for row in rows])

    def build_result_indexes(self, job_id: str, manifest: Track1BulkArtifactManifest) -> None:
        with _connect(self.path) as db:
            for group in db.execute("SELECT scope,community_id,count(*) count FROM bulk_communities WHERE job_id=? GROUP BY scope,community_id HAVING count(*)>=2", (job_id,)):
                shared = {signal: [] for signal in SIGNALS}
                coverage = {signal: 0 for signal in SIGNALS}
                members = "SELECT account_id FROM bulk_communities WHERE job_id=? AND scope=? AND community_id=?"
                for row in db.execute("""SELECT o.signal,o.token,count(DISTINCT o.account_id) owners
                    FROM bulk_token_owners o JOIN bulk_communities c ON c.job_id=o.job_id AND c.scope=o.scope AND c.account_id=o.account_id
                    WHERE o.job_id=? AND o.scope=? AND c.community_id=? AND o.projected=1
                    GROUP BY o.signal,o.token HAVING owners>=2""", (job_id, group["scope"], group["community_id"])):
                    shared[row["signal"]].append(row["token"])
                if not any(shared.values()):
                    continue
                for signal, values in shared.items():
                    if values:
                        placeholders = ",".join("?" for _ in values)
                        coverage[signal] = db.execute(f"SELECT count(DISTINCT account_id) FROM bulk_token_owners WHERE job_id=? AND scope=? AND signal=? AND token IN ({placeholders}) AND account_id IN ({members})", (job_id, group["scope"], signal, *values, job_id, group["scope"], group["community_id"])).fetchone()[0] / group["count"]
                ticket_hash = hashlib.sha256()
                for ticket in db.execute("""SELECT r.row_no FROM bulk_input_rows r JOIN bulk_communities c ON c.job_id=r.job_id AND c.scope=r.scope AND c.account_id=r.account_id
                    WHERE r.job_id=? AND c.community_id=? AND r.scope=? ORDER BY r.row_no""", (job_id, group["community_id"], group["scope"])):
                    ticket_hash.update(str(ticket["row_no"]).encode() + b",")
                cluster_id = "cluster-" + hashlib.sha256((job_id + group["scope"]).encode() + ticket_hash.digest()).hexdigest()
                db.execute("INSERT INTO bulk_clusters VALUES(?,?,?,?,?,?,?)", (job_id, cluster_id, group["community_id"], group["scope"], score_v2(group["count"], coverage), group["count"], json.dumps(shared, separators=(",", ":"))))
            # Auxiliary outputs stay explicit.  The bulk path does not invent quantity or observation facts.
            db.execute("INSERT OR REPLACE INTO bulk_aux_results VALUES(?,?,?)", (job_id, "[]", "[]"))

    def results(self, job_id: str, cursor: str | None, limit: int, member_limit: int) -> Track1BulkJobResults:
        status = self.status(job_id)
        if status.status != "COMPLETED":
            raise HTTPException(409, detail={"code": "RESULT_NOT_AVAILABLE", "status": status.status})
        offset = int(cursor or 0)
        with _connect(self.path) as db:
            rows = db.execute("SELECT * FROM bulk_clusters WHERE job_id=? ORDER BY cluster_id LIMIT ? OFFSET ?", (job_id, limit + 1, offset)).fetchall()
            clusters = []
            for row in rows[:limit]:
                tickets = db.execute("""SELECT row_json FROM bulk_input_rows r JOIN bulk_communities c ON c.job_id=r.job_id AND c.scope=r.scope AND c.account_id=r.account_id
                    WHERE r.job_id=? AND c.scope=? AND c.community_id=? ORDER BY r.row_no LIMIT ?""", (job_id, row["scope"], row["community_id"], member_limit + 1)).fetchall()
                members = [Track1LimitTicketInput.model_validate_json(ticket["row_json"]).model_dump(include={"account_id", "reservation_no", "event_id"}, by_alias=True) for ticket in tickets[:member_limit]]
                signals = json.loads(row["signals_json"])
                clusters.append(Track1BulkClusterSummary(clusterId=row["cluster_id"], riskScore=row["risk_score"], memberCount=row["member_count"], members=members, hasMoreMembers=len(tickets) > member_limit, paymentHashes=signals["payment_hash"], addressHashes=signals["address_hash"], deviceIdHashes=signals["device_id_hash"], ipHashes=signals["ip_hash"]))
            aux = db.execute("SELECT observations_json,limits_json FROM bulk_aux_results WHERE job_id=?", (job_id,)).fetchone()
        return Track1BulkJobResults(jobId=job_id, status="COMPLETED", algorithmVersion=status.algorithm_version or "", nextCursor=str(offset + limit) if len(rows) > limit else None, clusters=clusters, sharedSignalObservationsStatus="NOT_IMPLEMENTED", sharedSignalObservations=json.loads(aux["observations_json"]), purchaseLimitEvaluationsStatus="NOT_IMPLEMENTED", purchaseLimitEvaluations=json.loads(aux["limits_json"]))

    def member_page(self, job_id: str, cluster_id: str, cursor: str | None, limit: int) -> Track1BulkClusterMemberPage:
        status = self.status(job_id)
        if status.status != "COMPLETED":
            raise HTTPException(409, detail={"code": "RESULT_NOT_AVAILABLE", "status": status.status})
        offset = int(cursor or 0)
        with _connect(self.path) as db:
            cluster = db.execute("SELECT scope,community_id FROM bulk_clusters WHERE job_id=? AND cluster_id=?", (job_id, cluster_id)).fetchone()
            if not cluster:
                raise HTTPException(404, detail={"code": "CLUSTER_NOT_FOUND"})
            rows = db.execute("""SELECT row_json FROM bulk_input_rows r JOIN bulk_communities c ON c.job_id=r.job_id AND c.scope=r.scope AND c.account_id=r.account_id
                WHERE r.job_id=? AND c.scope=? AND c.community_id=? ORDER BY r.row_no LIMIT ? OFFSET ?""", (job_id, cluster["scope"], cluster["community_id"], limit + 1, offset)).fetchall()
        members = [Track1LimitTicketInput.model_validate_json(row["row_json"]).model_dump(include={"account_id", "reservation_no", "event_id"}, by_alias=True) for row in rows[:limit]]
        return Track1BulkClusterMemberPage(jobId=job_id, clusterId=cluster_id, nextCursor=str(offset + limit) if len(rows) > limit else None, members=members)


class Track1BulkJobService:
    def __init__(self, repository: BulkJobRepository, store: PrivateObjectStore, graph_service) -> None:
        self.repository, self.store, self.graph_service = repository, store, graph_service
        self.tasks: dict[str, asyncio.Task] = {}

    def submit(self, request, idempotency_key: str) -> Track1BulkJobAccepted:
        accepted = self.repository.create(idempotency_key, request.manifest)
        if accepted.status == "QUEUED" and accepted.job_id not in self.tasks:
            self.tasks[accepted.job_id] = asyncio.create_task(self._run(accepted.job_id))
        return accepted

    def retry(self, job_id: str) -> Track1BulkJobAccepted:
        accepted = self.repository.retry(job_id)
        self.tasks[job_id] = asyncio.create_task(self._run(job_id))
        return accepted

    async def _run(self, job_id: str) -> None:
        manifest = self.repository.manifest(job_id)
        self.repository.transition(job_id, "RUNNING")
        try:
            await asyncio.to_thread(
                self.repository.ingest, job_id,
                self.store.iter_bytes(manifest.object_key, manifest.object_version), manifest,
            )
            if self.repository.cancelled(job_id):
                raise JobCancelled()
            algorithm = await self.graph_service.analyze_bulk_sqlite(self.repository, job_id, manifest)
            await asyncio.to_thread(self.repository.build_result_indexes, job_id, manifest)
            self.repository.transition(job_id, "COMPLETED", algorithm=algorithm)
        except JobCancelled:
            self.repository.transition(job_id, "CANCELLED", failure="JOB_CANCELLED")
        except InputRejected as exc:
            self.repository.transition(job_id, "INPUT_REJECTED", failure=getattr(exc, "code", "INPUT_REJECTED"))
        except GdsResourceExhausted:
            self.repository.transition(job_id, "FAILED", failure="GDS_RESOURCE_EXHAUSTED")
        except Exception as exc:
            # Keep unavailable GDS distinct from a valid zero-community result.
            code = "GDS_UNAVAILABLE" if type(exc).__name__ == "GraphAnalysisUnavailable" else "BULK_ANALYSIS_FAILED"
            self.repository.transition(job_id, "FAILED", failure=code)
        finally:
            self.tasks.pop(job_id, None)
