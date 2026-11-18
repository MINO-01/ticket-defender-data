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
    Track1BulkClusterMemberPage, Track1BulkJobResults, Track1BulkJobStatus,
    Track1BulkPurchaseLimitEvaluationPage, Track1BulkPurchaseLimitEvaluationSummary,
    Track1BulkPurchaseLimitFindingPage, Track1BulkSharedSignalObservationPage,
    Track1AccountLimitFinding, Track1LimitTicketInput,
    Track1ReservationQuantity, Track1SharedSignalCorroboration, Track1SharedSignalObservation,
)
from services.track1_scoring import DEFAULT_PARAMETERS, SIGNALS, hub_minimum_accounts, is_hub, score_v2


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
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
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
                CREATE TABLE IF NOT EXISTS bulk_shared_signal_observations (
                  job_id TEXT NOT NULL, observation_id TEXT NOT NULL, scope TEXT NOT NULL,
                  signal TEXT NOT NULL, distinct_account_count INTEGER NOT NULL,
                  scope_account_count INTEGER NOT NULL, distinct_reservation_count INTEGER NOT NULL,
                  total_ticket_quantity INTEGER, ticket_quantity_status TEXT NOT NULL,
                  payment_shared_account_count INTEGER NOT NULL, device_shared_account_count INTEGER NOT NULL,
                  ip_network_type TEXT NOT NULL, purchase_window_seconds INTEGER,
                  excluded_from_louvain INTEGER NOT NULL,
                  PRIMARY KEY(job_id, observation_id),
                  FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_bulk_observations_page
                  ON bulk_shared_signal_observations(job_id, observation_id);
                CREATE TABLE IF NOT EXISTS bulk_purchase_limit_evaluations (
                  job_id TEXT NOT NULL, scope TEXT NOT NULL, internal_event_id TEXT NOT NULL,
                  event_id TEXT NOT NULL, performance_id TEXT NOT NULL, status TEXT NOT NULL,
                  policy_version TEXT, PRIMARY KEY(job_id, scope),
                  FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bulk_purchase_limit_findings (
                  job_id TEXT NOT NULL, scope TEXT NOT NULL, account_id TEXT NOT NULL,
                  effective_ticket_quantity INTEGER NOT NULL, max_tickets_per_account INTEGER NOT NULL,
                  policy_version TEXT NOT NULL, PRIMARY KEY(job_id, scope, account_id),
                  FOREIGN KEY(job_id, scope) REFERENCES bulk_purchase_limit_evaluations(job_id, scope) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bulk_purchase_limit_reservations (
                  job_id TEXT NOT NULL, scope TEXT NOT NULL, account_id TEXT NOT NULL,
                  reservation_no TEXT NOT NULL, effective_ticket_quantity INTEGER NOT NULL,
                  PRIMARY KEY(job_id, scope, account_id, reservation_no),
                  FOREIGN KEY(job_id, scope, account_id) REFERENCES bulk_purchase_limit_findings(job_id, scope, account_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_bulk_limit_findings_page
                  ON bulk_purchase_limit_findings(job_id, scope, account_id);
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
            # A fixed input maps to exactly one durable job.  A failed job is
            # deliberately returned too: callers must use that job's retry API
            # instead of creating a second job for the same immutable artifact.
            duplicate = db.execute("SELECT job_id, status FROM bulk_jobs WHERE fingerprint=?", (fingerprint,)).fetchone()
            if duplicate:
                return Track1BulkJobAccepted(jobId=duplicate["job_id"], status=duplicate["status"], existing=True)
            job_id, now = str(uuid.uuid4()), _now()
            try:
                db.execute("INSERT INTO bulk_jobs(job_id,idempotency_key,manifest_json,fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id, idempotency_key, json.dumps(payload, separators=(",", ":")), fingerprint, "QUEUED", now, now))
            except sqlite3.IntegrityError:
                # Another request may have inserted the same fingerprint after
                # this transaction read it.  Resolve it deterministically.
                duplicate = db.execute("SELECT job_id, status FROM bulk_jobs WHERE fingerprint=?", (fingerprint,)).fetchone()
                if duplicate:
                    return Track1BulkJobAccepted(jobId=duplicate["job_id"], status=duplicate["status"], existing=True)
                existing = db.execute("SELECT fingerprint FROM bulk_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if existing and existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"}) from None
                raise
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
        with _connect(self.path) as db:
            current = db.execute("SELECT status FROM bulk_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not current:
                raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
            changed = db.execute("""UPDATE bulk_jobs
                SET status='QUEUED', failure_code=NULL, cancel_requested=0, input_record_count=0, processed_record_count=0, updated_at=?
                WHERE job_id=? AND status IN ('FAILED','INPUT_REJECTED','CANCELLED','INTERRUPTED')""", (_now(), job_id)).rowcount
            if changed != 1:
                raise HTTPException(409, detail={"code": "JOB_NOT_RETRYABLE"})
            # All job-owned mutable input/derived state is reset in this same
            # transaction.  No other job shares these rows or is touched.
            for table in (
                "bulk_purchase_limit_reservations", "bulk_purchase_limit_findings", "bulk_purchase_limit_evaluations",
                "bulk_shared_signal_observations", "bulk_aux_results", "bulk_clusters", "bulk_communities",
                "bulk_token_owners", "bulk_accounts", "bulk_input_rows",
            ):
                db.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
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
                lines = carry.split(b"\n")
                carry = lines.pop()
                for line in lines:
                    if len(line.rstrip(b"\r")) > MAX_NDJSON_LINE_BYTES:
                        raise InputRejected("INPUT_ROW_TOO_LARGE")
                    row_no = self._ingest_line(db, job_id, line.rstrip(b"\r"), row_no, manifest, scopes)
                    if row_no % 500 == 0:
                        db.commit()
                        if self.cancelled(job_id):
                            raise JobCancelled()
                # Only an unfinished line can remain in carry.  A large chunk
                # containing many normal lines is valid NDJSON and must not be
                # rejected as one oversized record.
                if len(carry) > MAX_NDJSON_LINE_BYTES:
                    raise InputRejected("INPUT_ROW_TOO_LARGE")
            if carry:
                if len(carry.rstrip(b"\r")) > MAX_NDJSON_LINE_BYTES:
                    raise InputRejected("INPUT_ROW_TOO_LARGE")
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
        if scope.quantity_source_version != manifest.source_version or scope.quantity_as_of != manifest.source_as_of:
            raise InputRejected("INPUT_SCOPE_PROVENANCE_MISMATCH")
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

    @staticmethod
    def _scope_quantity_is_trustworthy(db, job_id: str, scope) -> bool:
        """SQLite equivalent of the v2 quantity proof, without materializing tickets."""
        if not scope.quantity_is_complete:
            return False
        summary = db.execute("""SELECT
              count(*) AS total,
              sum(CASE WHEN json_extract(row_json, '$.quantityInputType') IS NOT NULL THEN 1 ELSE 0 END) AS typed,
              sum(CASE WHEN json_extract(row_json, '$.ticketStatus') IS NOT 'ACTIVE'
                         OR json_extract(row_json, '$.sourceVersion') IS NOT ?
                         OR json_extract(row_json, '$.sourceAsOf') IS NULL
                         OR julianday(json_extract(row_json, '$.sourceAsOf')) > julianday(?)
                         OR julianday(json_extract(row_json, '$.purchasedAt')) > julianday(json_extract(row_json, '$.sourceAsOf'))
                       THEN 1 ELSE 0 END) AS invalid
            FROM bulk_input_rows WHERE job_id=? AND scope=?""",
            (scope.quantity_source_version, scope.quantity_as_of.isoformat().replace("+00:00", "Z"), job_id, scope.performance_id.lower()),
        ).fetchone()
        if summary["total"] == 0:
            return True
        # v2 permits entirely legacy rows with no provenance; bulk ingestion
        # requires source provenance, so any untyped bulk row is incomplete.
        if summary["typed"] != summary["total"] or summary["invalid"]:
            return False
        ambiguous = db.execute("""SELECT 1 FROM bulk_input_rows
            WHERE job_id=? AND scope=? AND ticket_reference=''
            GROUP BY account_id,reservation_no HAVING count(*) > 1 LIMIT 1""",
            (job_id, scope.performance_id.lower()),
        ).fetchone()
        if ambiguous is not None:
            return False
        # Match v2's reservation_of_reference proof: a ticket reference must
        # identify one account/reservation pair within its performance.
        cross_owner_reference = db.execute("""SELECT 1 FROM (
                SELECT ticket_reference,account_id,reservation_no FROM bulk_input_rows
                WHERE job_id=? AND scope=? AND ticket_reference<>''
                GROUP BY ticket_reference,account_id,reservation_no
            ) references_by_owner
            GROUP BY ticket_reference HAVING count(*) > 1 LIMIT 1""",
            (job_id, scope.performance_id.lower()),
        ).fetchone()
        return cross_owner_reference is None

    @staticmethod
    def _shared_corroboration(db, job_id: str, scope: str, signal: str, token: str) -> tuple[int, int]:
        """Count only observed owners that themselves share payment/device signals."""
        values = []
        for corroborating_signal in ("payment_hash", "device_id_hash"):
            row = db.execute("""WITH observed_accounts AS (
                    SELECT account_id FROM bulk_token_owners
                    WHERE job_id=? AND scope=? AND signal=? AND token=?
                ), shared_observed_tokens AS (
                    SELECT evidence.token FROM bulk_token_owners evidence
                    JOIN observed_accounts observed ON observed.account_id=evidence.account_id
                    WHERE evidence.job_id=? AND evidence.scope=? AND evidence.signal=?
                    GROUP BY evidence.token HAVING count(DISTINCT evidence.account_id) >= 2
                )
                SELECT count(DISTINCT evidence.account_id) AS n FROM bulk_token_owners evidence
                JOIN observed_accounts observed ON observed.account_id=evidence.account_id
                JOIN shared_observed_tokens shared ON shared.token=evidence.token
                WHERE evidence.job_id=? AND evidence.scope=? AND evidence.signal=?""",
                (job_id, scope, signal, token, job_id, scope, corroborating_signal,
                 job_id, scope, corroborating_signal)).fetchone()
            values.append(row["n"])
        return values[0], values[1]

    def _build_auxiliary_results(self, db, job_id: str, manifest: Track1BulkArtifactManifest) -> None:
        """Persist opt-in results atomically with the cluster result indexes."""
        for table in (
            "bulk_purchase_limit_reservations", "bulk_purchase_limit_findings", "bulk_purchase_limit_evaluations",
            "bulk_shared_signal_observations", "bulk_aux_results",
        ):
            db.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
        account_counts = {row["scope"]: row["n"] for row in db.execute(
            "SELECT scope,count(*) AS n FROM bulk_accounts WHERE job_id=? GROUP BY scope", (job_id,)
        )}
        verified_scopes = {
            scope.performance_id.lower() for scope in manifest.performance_scopes
            if self._scope_quantity_is_trustworthy(db, job_id, scope)
        }
        observed = db.execute("""SELECT scope,signal,token,count(*) AS owner_count
            FROM bulk_token_owners WHERE job_id=? AND signal IN ('ip_hash','address_hash')
            GROUP BY scope,signal,token ORDER BY scope,signal,token""", (job_id,))
        for item in observed:
            minimum = hub_minimum_accounts(item["signal"], DEFAULT_PARAMETERS)
            if minimum is None or item["owner_count"] < minimum:
                continue
            scope = item["scope"]
            json_signal = "$." + {"ip_hash": "ipHash", "address_hash": "addressHash"}[item["signal"]]
            evidence = db.execute("""SELECT
                  count(DISTINCT account_id || char(31) || reservation_no) AS reservation_count,
                  sum(CAST(json_extract(row_json, '$.effectiveTicketQuantity') AS INTEGER)) AS quantity,
                  min(julianday(json_extract(row_json, '$.purchasedAt'))) AS first_purchase,
                  max(julianday(json_extract(row_json, '$.purchasedAt'))) AS last_purchase
                FROM bulk_input_rows WHERE job_id=? AND scope=? AND json_extract(row_json, ?) = ?""",
                (job_id, scope, json_signal, item["token"]),
            ).fetchone()
            verified = scope in verified_scopes
            quantity = evidence["quantity"] if verified else None
            first_purchase = evidence["first_purchase"] if verified else None
            last_purchase = evidence["last_purchase"] if verified else None
            networks = {
                row["network"] for row in db.execute("""SELECT DISTINCT json_extract(row_json, '$.ipNetworkType') AS network
                    FROM bulk_input_rows WHERE job_id=? AND scope=? AND json_extract(row_json, ?) = ? LIMIT 2""",
                    (job_id, scope, json_signal, item["token"])
                )
            } if item["signal"] == "ip_hash" else {None}
            network = next(iter(networks)) if len(networks) == 1 and None not in networks else "UNKNOWN"
            payment_count, device_count = self._shared_corroboration(db, job_id, scope, item["signal"], item["token"])
            identity = json.dumps([job_id, scope, item["signal"], item["token"]], separators=(",", ":"))
            observation_id = "obs-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
            db.execute("""INSERT INTO bulk_shared_signal_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                job_id, observation_id, scope, item["signal"], item["owner_count"], account_counts[scope],
                evidence["reservation_count"], quantity,
                "VERIFIED" if verified else "UNAVAILABLE", payment_count, device_count, network,
                int((last_purchase - first_purchase) * 86_400) if first_purchase is not None and last_purchase is not None else None,
                int(is_hub(item["signal"], item["owner_count"], DEFAULT_PARAMETERS, account_counts[scope])),
            ))

        for scope in manifest.performance_scopes:
            scope_key, policy = scope.performance_id.lower(), scope.policy
            status = "EVALUATED"
            if policy is None:
                status = "POLICY_NOT_CONFIRMED"
            elif not self._scope_quantity_is_trustworthy(db, job_id, scope):
                status = "QUANTITY_DATA_INCOMPLETE"
            elif db.execute("""SELECT 1 FROM bulk_input_rows WHERE job_id=? AND scope=?
                    AND (julianday(json_extract(row_json, '$.purchasedAt')) < julianday(?) OR
                         (? IS NOT NULL AND julianday(json_extract(row_json, '$.purchasedAt')) >= julianday(?))) LIMIT 1""",
                    (job_id, scope_key, policy.effective_from.isoformat().replace("+00:00", "Z"),
                     policy.effective_until is not None,
                     policy.effective_until.isoformat().replace("+00:00", "Z") if policy.effective_until else None),
                ).fetchone():
                status = "POLICY_TIME_NOT_APPLICABLE"
            db.execute("INSERT INTO bulk_purchase_limit_evaluations VALUES(?,?,?,?,?,?,?)", (
                job_id, scope_key, str(scope.internal_event_id), scope.event_id, str(scope.performance_id), status,
                policy.policy_version if policy else None,
            ))
            if status != "EVALUATED":
                continue
            findings = db.execute("""SELECT account_id, sum(CAST(json_extract(row_json, '$.effectiveTicketQuantity') AS INTEGER)) AS quantity
                FROM bulk_input_rows WHERE job_id=? AND scope=? GROUP BY account_id HAVING quantity > ?""",
                (job_id, scope_key, policy.max_tickets_per_account),
            )
            for finding in findings:
                db.execute("INSERT INTO bulk_purchase_limit_findings VALUES(?,?,?,?,?,?)", (
                    job_id, scope_key, finding["account_id"], finding["quantity"], policy.max_tickets_per_account, policy.policy_version,
                ))
                reservations = db.execute("""SELECT reservation_no, sum(CAST(json_extract(row_json, '$.effectiveTicketQuantity') AS INTEGER)) AS quantity
                    FROM bulk_input_rows WHERE job_id=? AND scope=? AND account_id=? GROUP BY reservation_no""",
                    (job_id, scope_key, finding["account_id"]),
                )
                db.executemany("INSERT INTO bulk_purchase_limit_reservations VALUES(?,?,?,?,?)", (
                    (job_id, scope_key, finding["account_id"], row["reservation_no"], row["quantity"])
                    for row in reservations
                ))
        # Legacy result response deliberately remains the Backend #97 shape.
        db.execute("INSERT INTO bulk_aux_results VALUES(?,?,?)", (job_id, "[]", "[]"))

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
            self._build_auxiliary_results(db, job_id, manifest)

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
        return Track1BulkJobResults(jobId=job_id, status="COMPLETED", algorithmVersion=status.algorithm_version or "", nextCursor=str(offset + limit) if len(rows) > limit else None, clusters=clusters, sharedSignalObservationsStatus="NOT_IMPLEMENTED", sharedSignalObservations=json.loads(aux["observations_json"]) if aux else [], purchaseLimitEvaluationsStatus="NOT_IMPLEMENTED", purchaseLimitEvaluations=json.loads(aux["limits_json"]) if aux else [])

    def shared_signal_observation_page(self, job_id: str, cursor: str | None, limit: int) -> Track1BulkSharedSignalObservationPage:
        status = self.status(job_id)
        if status.status != "COMPLETED":
            raise HTTPException(409, detail={"code": "RESULT_NOT_AVAILABLE", "status": status.status})
        offset = int(cursor or 0)
        with _connect(self.path) as db:
            rows = db.execute("""SELECT * FROM bulk_shared_signal_observations WHERE job_id=?
                ORDER BY observation_id LIMIT ? OFFSET ?""", (job_id, limit + 1, offset)).fetchall()
        observations = [Track1SharedSignalObservation(
            observationId=row["observation_id"], scopeType="PERFORMANCE", scopeId=row["scope"],
            signalType="IP" if row["signal"] == "ip_hash" else "ADDRESS",
            distinctAccountCount=row["distinct_account_count"], scopeAccountCount=row["scope_account_count"],
            distinctReservationCount=row["distinct_reservation_count"], totalTicketQuantity=row["total_ticket_quantity"],
            ticketQuantityStatus=row["ticket_quantity_status"],
            corroboration=Track1SharedSignalCorroboration(
                paymentSharedAccountCount=row["payment_shared_account_count"],
                deviceSharedAccountCount=row["device_shared_account_count"],
            ), ipNetworkType=row["ip_network_type"], purchaseWindowSeconds=row["purchase_window_seconds"],
            excludedFromLouvain=bool(row["excluded_from_louvain"]),
        ) for row in rows[:limit]]
        return Track1BulkSharedSignalObservationPage(
            jobId=job_id, status="COMPLETED", nextCursor=str(offset + limit) if len(rows) > limit else None,
            sharedSignalObservationsStatus="COMPLETED", sharedSignalObservations=observations,
        )

    def purchase_limit_evaluation_page(self, job_id: str, cursor: str | None, limit: int) -> Track1BulkPurchaseLimitEvaluationPage:
        status = self.status(job_id)
        if status.status != "COMPLETED":
            raise HTTPException(409, detail={"code": "RESULT_NOT_AVAILABLE", "status": status.status})
        offset = int(cursor or 0)
        with _connect(self.path) as db:
            rows = db.execute("""SELECT * FROM bulk_purchase_limit_evaluations WHERE job_id=?
                ORDER BY scope LIMIT ? OFFSET ?""", (job_id, limit + 1, offset)).fetchall()
            evaluations = []
            for row in rows[:limit]:
                finding_count = db.execute("""SELECT count(*) FROM bulk_purchase_limit_findings
                    WHERE job_id=? AND scope=?""", (job_id, row["scope"])).fetchone()[0]
                evaluations.append(Track1BulkPurchaseLimitEvaluationSummary(
                    internalEventId=row["internal_event_id"], eventId=row["event_id"], performanceId=row["performance_id"],
                    status=row["status"], policyVersion=row["policy_version"], accountLimitFindingCount=finding_count,
                ))
        return Track1BulkPurchaseLimitEvaluationPage(
            jobId=job_id, status="COMPLETED", nextCursor=str(offset + limit) if len(rows) > limit else None,
            purchaseLimitEvaluationsStatus="COMPLETED", purchaseLimitEvaluations=evaluations,
        )

    def purchase_limit_finding_page(self, job_id: str, performance_id: str, cursor: str | None, limit: int) -> Track1BulkPurchaseLimitFindingPage:
        status = self.status(job_id)
        if status.status != "COMPLETED":
            raise HTTPException(409, detail={"code": "RESULT_NOT_AVAILABLE", "status": status.status})
        offset = int(cursor or 0)
        with _connect(self.path) as db:
            evaluation = db.execute("""SELECT * FROM bulk_purchase_limit_evaluations
                WHERE job_id=? AND lower(performance_id)=lower(?)""", (job_id, performance_id)).fetchone()
            if not evaluation:
                raise HTTPException(404, detail={"code": "PERFORMANCE_NOT_FOUND"})
            rows = db.execute("""SELECT * FROM bulk_purchase_limit_findings WHERE job_id=? AND scope=?
                ORDER BY account_id LIMIT ? OFFSET ?""", (job_id, evaluation["scope"], limit + 1, offset)).fetchall()
            findings = []
            for finding in rows[:limit]:
                reservations = db.execute("""SELECT reservation_no,effective_ticket_quantity
                    FROM bulk_purchase_limit_reservations WHERE job_id=? AND scope=? AND account_id=?
                    ORDER BY reservation_no""", (job_id, evaluation["scope"], finding["account_id"])).fetchall()
                findings.append(Track1AccountLimitFinding(
                    accountId=finding["account_id"], effectiveTicketQuantity=finding["effective_ticket_quantity"],
                    maxTicketsPerAccount=finding["max_tickets_per_account"], policyVersion=finding["policy_version"],
                    reservationQuantities=[Track1ReservationQuantity(
                        reservationNo=item["reservation_no"], effectiveTicketQuantity=item["effective_ticket_quantity"],
                    ) for item in reservations],
                ))
        return Track1BulkPurchaseLimitFindingPage(
            jobId=job_id, performanceId=evaluation["performance_id"], status="COMPLETED",
            nextCursor=str(offset + limit) if len(rows) > limit else None,
            evaluationStatus=evaluation["status"], policyVersion=evaluation["policy_version"], accountLimitFindings=findings,
        )

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
