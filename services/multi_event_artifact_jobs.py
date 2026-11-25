"""Durable NDJSON-backed multi-event analysis jobs.

This is separate from the small JSON `/multi-event/analyze` compatibility path.
Input rows and results are staged in SQLite; the worker never builds a Python
list containing the entire ledger or result set.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Iterable
import uuid

from fastapi import HTTPException

from schemas.multi_event import (
    MultiEventArtifactJobAccepted,
    MultiEventArtifactJobStatus,
    MultiEventArtifactManifest,
    MultiEventCandidatePage,
    MultiEventCandidateSummary,
    MultiEventLedgerScope,
    MultiEventMemberPage,
    MultiEventRelationshipPage,
    MultiEventRelationshipSummary,
    MultiEventScoreBreakdown,
    MultiEventTicketInput,
    RETIRED_SCORE_VERSIONS,
    STORED_CONTEXT,
)
from services.multi_event_candidates import (
    STABLE_ACCOUNT_COCLUSTER,
    RecurrenceComparisonLimit,
    cocluster_comparison_count,
    cocluster_pairs,
    evaluate_candidate,
    link_inputs,
    recurrence_pair_allowed,
    require_comparison_budget,
    token_damping,
)
from services.track1_bulk_jobs import (
    GdsResourceExhausted,
    InputRejected,
    ObjectVersionMismatch,
    PrivateObjectStore,
)
from services.track1_scoring_exp2 import (
    BASE_SIGNALS,
    DEFAULT_EXP2_PARAMETERS,
    STRONG_RECURRENCE_SIGNALS,
    damping,
    ip_window_token,
    quantity_context,
)


MAX_NDJSON_LINE_BYTES = 1_048_576
MAX_PROJECTED_EDGES = 5_000_000
MAX_CANDIDATES = 10_000
MAX_CANDIDATE_MEMBERS = 50_000
MAX_CANDIDATE_MEMBER_ROWS = 5_000_000
MAX_RECURRENCE_COMPARISONS = 2_000_000
MAX_CANDIDATE_EVIDENCE_GROUPS = 50_000
MAX_CANDIDATE_EVIDENCE_ROWS = 5_000_000
MAX_RELATIONSHIP_MEMBER_ROWS = 5_000_000
MAX_QUANTITY_REASONS = 8
MAX_ATTEMPTS = 3
MAX_PENDING_JOBS = 100
PAGE_SIZE_MAX = 500
DEFAULT_MAX_ARTIFACT_BYTES = 1_073_741_824
logger = logging.getLogger(__name__)


class MultiEventResourceLimit(GdsResourceExhausted):
    """A bounded multi-event resource limit with an explicit failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_id(analysis_id: str, accounts: Iterable[str]) -> str:
    raw = json.dumps([analysis_id, sorted(accounts)], separators=(",", ":"))
    return "multi-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _membership_digest(accounts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for account in sorted(accounts):
        digest.update(account.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _public_scope(scope) -> MultiEventLedgerScope:
    """Strip the internal event UUID used only for the ledger-set digest."""
    return MultiEventLedgerScope(
        eventId=scope.event_id, performanceId=scope.performance_id,
        ledgerVersionId=scope.ledger_version_id, sourceBatchId=scope.source_batch_id,
        policySnapshotId=scope.policy_snapshot_id,
    )


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


class MultiEventArtifactJobRepository:
    """SQLite job state, streaming input indexes, and page-ready result records."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.max_artifact_bytes = int(os.getenv("MULTI_EVENT_ARTIFACT_MAX_BYTES", str(DEFAULT_MAX_ARTIFACT_BYTES)))
        if self.max_artifact_bytes < 1:
            raise RuntimeError("MULTI_EVENT_ARTIFACT_MAX_BYTES must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mei_artifact_jobs (
                  job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                  manifest_json TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED','INPUT_REJECTED','INTERRUPTED')),
                  input_record_count INTEGER NOT NULL DEFAULT 0 CHECK(input_record_count BETWEEN 0 AND 400000),
                  processed_record_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_record_count BETWEEN 0 AND 400000),
                  candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count BETWEEN 0 AND 10000),
                  algorithm_version TEXT, failure_code TEXT, attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 3),
                  supplier_namespace TEXT, hmac_key_version TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_scopes (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, event_id TEXT NOT NULL,
                  performance_id TEXT NOT NULL, ledger_version_id TEXT NOT NULL,
                  scope_json TEXT NOT NULL, PRIMARY KEY(job_id,scope_id),
                  UNIQUE(job_id,event_id,performance_id,ledger_version_id),
                  FOREIGN KEY(job_id) REFERENCES mei_artifact_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_rows (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, row_ordinal INTEGER NOT NULL,
                  account_id TEXT NOT NULL, reservation_no TEXT NOT NULL,
                  PRIMARY KEY(job_id,scope_id,row_ordinal),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_accounts (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                  PRIMARY KEY(job_id,scope_id,account_id),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_token_owners (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, signal TEXT NOT NULL,
                  token TEXT NOT NULL, account_id TEXT NOT NULL,
                  projected INTEGER NOT NULL DEFAULT 0 CHECK(projected IN (0,1)),
                  weight REAL,
                  PRIMARY KEY(job_id,scope_id,signal,token,account_id),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_mei_token_lookup
                  ON mei_artifact_token_owners(job_id,signal,token,scope_id,account_id);
                CREATE INDEX IF NOT EXISTS idx_mei_token_account
                  ON mei_artifact_token_owners(job_id,account_id,scope_id);
                CREATE TABLE IF NOT EXISTS mei_artifact_ip_types (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, token TEXT NOT NULL,
                  account_id TEXT NOT NULL, network_type TEXT NOT NULL,
                  PRIMARY KEY(job_id,scope_id,token,account_id,network_type),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_quantities (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                  row_ordinal INTEGER NOT NULL, quantity INTEGER, purchase_limit INTEGER,
                  snapshot_id TEXT, policy_version TEXT, source_version TEXT, row_source_version TEXT,
                  status TEXT NOT NULL, reason TEXT,
                  PRIMARY KEY(job_id,scope_id,account_id,row_ordinal),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_communities (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                  community_id INTEGER NOT NULL,
                  PRIMARY KEY(job_id,scope_id,account_id),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_quantity_contexts (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                  value REAL NOT NULL CHECK(value BETWEEN 0 AND 1), status TEXT NOT NULL,
                  reason TEXT,
                  PRIMARY KEY(job_id,scope_id,account_id),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_excluded_tokens (
                  job_id TEXT NOT NULL, scope_id INTEGER NOT NULL, signal TEXT NOT NULL,
                  token TEXT NOT NULL, owner_count INTEGER NOT NULL, owner_digest TEXT NOT NULL,
                  PRIMARY KEY(job_id,scope_id,signal,token),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_evidence_groups (
                  job_id TEXT NOT NULL, group_id TEXT NOT NULL, scope_id INTEGER NOT NULL,
                  signal TEXT NOT NULL, token TEXT NOT NULL, community_id INTEGER NOT NULL,
                  owner_count INTEGER NOT NULL, damping REAL NOT NULL,
                  PRIMARY KEY(job_id,group_id),
                  UNIQUE(job_id,scope_id,signal,token,community_id),
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_mei_evidence_token
                  ON mei_artifact_evidence_groups(job_id,signal,token,scope_id);
                CREATE TABLE IF NOT EXISTS mei_artifact_evidence_members (
                  job_id TEXT NOT NULL, group_id TEXT NOT NULL, account_id TEXT NOT NULL,
                  PRIMARY KEY(job_id,group_id,account_id),
                  FOREIGN KEY(job_id,group_id) REFERENCES mei_artifact_evidence_groups(job_id,group_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_candidates (
                  job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, priority_band TEXT NOT NULL,
                  score REAL NOT NULL, distinct_account_count INTEGER NOT NULL,
                  distinct_event_count INTEGER NOT NULL, distinct_performance_count INTEGER NOT NULL,
                  verified_relationship_count INTEGER NOT NULL, summary_json TEXT NOT NULL,
                  PRIMARY KEY(job_id,candidate_id),
                  FOREIGN KEY(job_id) REFERENCES mei_artifact_jobs(job_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_mei_candidate_page
                  ON mei_artifact_candidates(job_id,score DESC,candidate_id);
                CREATE TABLE IF NOT EXISTS mei_artifact_candidate_members (
                  job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, account_id TEXT NOT NULL,
                  PRIMARY KEY(job_id,candidate_id,account_id),
                  FOREIGN KEY(job_id,candidate_id) REFERENCES mei_artifact_candidates(job_id,candidate_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_candidate_evidence (
                  job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, group_id TEXT NOT NULL,
                  PRIMARY KEY(job_id,candidate_id,group_id),
                  FOREIGN KEY(job_id,candidate_id) REFERENCES mei_artifact_candidates(job_id,candidate_id) ON DELETE CASCADE,
                  FOREIGN KEY(job_id,group_id) REFERENCES mei_artifact_evidence_groups(job_id,group_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_candidate_scopes (
                  job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, scope_id INTEGER NOT NULL,
                  PRIMARY KEY(job_id,candidate_id,scope_id),
                  FOREIGN KEY(job_id,candidate_id) REFERENCES mei_artifact_candidates(job_id,candidate_id) ON DELETE CASCADE,
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_cocluster_members (
                  job_id TEXT NOT NULL, left_scope_id INTEGER NOT NULL, left_community_id INTEGER NOT NULL,
                  right_scope_id INTEGER NOT NULL, right_community_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                  PRIMARY KEY(job_id,left_scope_id,left_community_id,right_scope_id,right_community_id,account_id),
                  FOREIGN KEY(job_id) REFERENCES mei_artifact_jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_candidate_cocluster (
                  job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, scope_id INTEGER NOT NULL,
                  PRIMARY KEY(job_id,candidate_id,scope_id),
                  FOREIGN KEY(job_id,candidate_id) REFERENCES mei_artifact_candidates(job_id,candidate_id) ON DELETE CASCADE,
                  FOREIGN KEY(job_id,scope_id) REFERENCES mei_artifact_scopes(job_id,scope_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mei_artifact_relationships (
                  job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, relationship_id TEXT NOT NULL,
                  signal_type TEXT NOT NULL, account_count INTEGER NOT NULL,
                  source_scope_count INTEGER NOT NULL, source_scopes_json TEXT NOT NULL,
                  PRIMARY KEY(job_id,candidate_id,relationship_id),
                  UNIQUE(job_id,candidate_id,signal_type),
                  FOREIGN KEY(job_id,candidate_id) REFERENCES mei_artifact_candidates(job_id,candidate_id) ON DELETE CASCADE
                );
            """)

    def recover_interrupted(self) -> None:
        with _connect(self.path) as db:
            db.execute("UPDATE mei_artifact_jobs SET status='INTERRUPTED',failure_code='PROCESS_RESTARTED',updated_at=? WHERE status='RUNNING'", (_now(),))

    def queued_job_ids(self) -> list[str]:
        with _connect(self.path) as db:
            return [row["job_id"] for row in db.execute(
                "SELECT job_id FROM mei_artifact_jobs WHERE status='QUEUED' ORDER BY created_at,job_id LIMIT ?",
                (MAX_PENDING_JOBS,),
            )]

    def create(self, idempotency_key: str, manifest: MultiEventArtifactManifest) -> MultiEventArtifactJobAccepted:
        payload = manifest.model_dump(mode="json", by_alias=True)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with _connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT job_id,fingerprint,status FROM mei_artifact_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise HTTPException(409, detail={"code": "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST"})
                return MultiEventArtifactJobAccepted(jobId=row["job_id"], status=row["status"], existing=True)
            row = db.execute("SELECT job_id,status FROM mei_artifact_jobs WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row:
                return MultiEventArtifactJobAccepted(jobId=row["job_id"], status=row["status"], existing=True)
            pending = db.execute("SELECT count(*) FROM mei_artifact_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
            if pending >= MAX_PENDING_JOBS:
                raise HTTPException(429, detail={"code": "MULTI_EVENT_JOB_QUEUE_FULL"})
            job_id, now = str(uuid.uuid4()), _now()
            try:
                db.execute("INSERT INTO mei_artifact_jobs(job_id,idempotency_key,manifest_json,fingerprint,status,created_at,updated_at) VALUES(?,?,?,?, 'QUEUED',?,?)",
                           (job_id, idempotency_key, serialized, fingerprint, now, now))
                for scope_id, scope in enumerate(manifest.ledger_scopes):
                    db.execute("INSERT INTO mei_artifact_scopes(job_id,scope_id,event_id,performance_id,ledger_version_id,scope_json) VALUES(?,?,?,?,?,?)",
                               (job_id, scope_id, scope.event_id, scope.performance_id, scope.ledger_version_id, scope.model_dump_json(by_alias=True)))
            except sqlite3.IntegrityError:
                row = db.execute("SELECT job_id,fingerprint,status FROM mei_artifact_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row and row["fingerprint"] == fingerprint:
                    return MultiEventArtifactJobAccepted(jobId=row["job_id"], status=row["status"], existing=True)
                raise HTTPException(409, detail={"code": "MULTI_EVENT_JOB_CONFLICT"}) from None
        return MultiEventArtifactJobAccepted(jobId=job_id, status="QUEUED", existing=False)

    def manifest(self, job_id: str) -> MultiEventArtifactManifest:
        with _connect(self.path) as db:
            row = db.execute("SELECT manifest_json FROM mei_artifact_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail={"code": "MULTI_EVENT_JOB_NOT_FOUND"})
        return MultiEventArtifactManifest.model_validate_json(row["manifest_json"], context=STORED_CONTEXT)

    def status(self, job_id: str) -> MultiEventArtifactJobStatus:
        with _connect(self.path) as db:
            row = db.execute("SELECT status,input_record_count,processed_record_count,candidate_count,algorithm_version,failure_code,manifest_json,created_at,updated_at FROM mei_artifact_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail={"code": "MULTI_EVENT_JOB_NOT_FOUND"})
        manifest = MultiEventArtifactManifest.model_validate_json(row["manifest_json"], context=STORED_CONTEXT)
        return MultiEventArtifactJobStatus(
            jobId=job_id, status=row["status"], inputRecordCount=row["input_record_count"],
            processedRecordCount=row["processed_record_count"], candidateCount=row["candidate_count"],
            algorithmVersion=row["algorithm_version"], failureCode=row["failure_code"],
            ledgerVersionSetDigest=manifest.ledger_version_set_digest,
            createdAt=datetime.fromisoformat(row["created_at"]), updatedAt=datetime.fromisoformat(row["updated_at"]),
        )

    def start(self, job_id: str) -> bool:
        with _connect(self.path) as db:
            changed = db.execute("UPDATE mei_artifact_jobs SET status='RUNNING',failure_code=NULL,attempt_count=attempt_count+1,updated_at=? WHERE job_id=? AND status='QUEUED' AND attempt_count<3", (_now(), job_id)).rowcount
        return changed == 1

    def retry(self, job_id: str) -> MultiEventArtifactJobAccepted:
        with _connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,attempt_count FROM mei_artifact_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise HTTPException(404, detail={"code": "MULTI_EVENT_JOB_NOT_FOUND"})
            if row["status"] not in {"FAILED", "INTERRUPTED"} or row["attempt_count"] >= 3:
                raise HTTPException(409, detail={"code": "MULTI_EVENT_JOB_NOT_RETRYABLE"})
            pending = db.execute("SELECT count(*) FROM mei_artifact_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
            if pending >= MAX_PENDING_JOBS:
                raise HTTPException(429, detail={"code": "MULTI_EVENT_JOB_QUEUE_FULL"})
            changed = db.execute("UPDATE mei_artifact_jobs SET status='QUEUED',failure_code=NULL,algorithm_version=NULL,input_record_count=0,processed_record_count=0,candidate_count=0,attempt_count=attempt_count,updated_at=? WHERE job_id=? AND status IN ('FAILED','INTERRUPTED') AND attempt_count<3", (_now(), job_id)).rowcount
            if changed != 1:
                raise HTTPException(409, detail={"code": "MULTI_EVENT_JOB_NOT_RETRYABLE"})
        return MultiEventArtifactJobAccepted(jobId=job_id, status="QUEUED", existing=True)

    def transition(self, job_id: str, status: str, failure: str | None = None, algorithm: str | None = None) -> None:
        if status not in {"COMPLETED", "FAILED", "INPUT_REJECTED", "INTERRUPTED"}:
            raise ValueError("invalid terminal multi-event job state")
        with _connect(self.path) as db:
            changed = db.execute("UPDATE mei_artifact_jobs SET status=?,failure_code=?,algorithm_version=COALESCE(?,algorithm_version),updated_at=? WHERE job_id=? AND status='RUNNING'",
                                 (status, failure, algorithm, _now(), job_id)).rowcount
        if changed != 1:
            raise RuntimeError("MULTI_EVENT_JOB_STATE_CONFLICT")

    def clear_stage(self, job_id: str) -> None:
        with _connect(self.path) as db:
            for table in ("mei_artifact_candidate_cocluster", "mei_artifact_cocluster_members", "mei_artifact_candidate_evidence", "mei_artifact_candidate_scopes", "mei_artifact_candidate_members", "mei_artifact_relationships", "mei_artifact_candidates", "mei_artifact_evidence_members", "mei_artifact_evidence_groups", "mei_artifact_communities", "mei_artifact_quantity_contexts", "mei_artifact_excluded_tokens", "mei_artifact_quantities", "mei_artifact_ip_types", "mei_artifact_token_owners", "mei_artifact_accounts", "mei_artifact_rows"):
                db.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            db.execute("UPDATE mei_artifact_jobs SET input_record_count=0,processed_record_count=0,candidate_count=0,supplier_namespace=NULL,hmac_key_version=NULL WHERE job_id=?", (job_id,))

    def ingest(self, job_id: str, chunks: Iterable[bytes], manifest: MultiEventArtifactManifest) -> None:
        digest, carry, row_no = hashlib.sha256(), b"", 0
        byte_count = 0
        scopes = {scope.key: index for index, scope in enumerate(manifest.ledger_scopes)}
        supplier_namespace = None
        key_version = None
        with _connect(self.path) as db:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise InputRejected("INPUT_CHUNK_INVALID")
                byte_count += len(chunk)
                if byte_count > self.max_artifact_bytes:
                    raise InputRejected("MULTI_EVENT_ARTIFACT_TOO_LARGE")
                digest.update(chunk)
                carry += chunk
                lines = carry.split(b"\n")
                carry = lines.pop()
                for line in lines:
                    if len(line.rstrip(b"\r")) > MAX_NDJSON_LINE_BYTES:
                        raise InputRejected("INPUT_ROW_TOO_LARGE")
                    row_no, supplier_namespace, key_version = self._ingest_line(
                        db, job_id, line.rstrip(b"\r"), row_no, manifest, scopes, supplier_namespace, key_version,
                    )
                    if row_no % 500 == 0:
                        db.commit()
                if len(carry) > MAX_NDJSON_LINE_BYTES:
                    raise InputRejected("INPUT_ROW_TOO_LARGE")
            if carry:
                if len(carry.rstrip(b"\r")) > MAX_NDJSON_LINE_BYTES:
                    raise InputRejected("INPUT_ROW_TOO_LARGE")
                row_no, supplier_namespace, key_version = self._ingest_line(
                    db, job_id, carry.rstrip(b"\r"), row_no, manifest, scopes, supplier_namespace, key_version,
                )
            if digest.hexdigest() != manifest.sha256:
                raise InputRejected("SHA256_MISMATCH")
            if row_no != manifest.record_count:
                raise InputRejected("RECORD_COUNT_MISMATCH")
            if not supplier_namespace or not key_version:
                raise InputRejected("IDENTITY_CONTRACT_UNAVAILABLE")
            self._finalize_ip_windows(db, job_id)
            db.execute("UPDATE mei_artifact_jobs SET input_record_count=?,processed_record_count=?,supplier_namespace=?,hmac_key_version=?,updated_at=? WHERE job_id=? AND status='RUNNING'",
                       (row_no, row_no, supplier_namespace, key_version, _now(), job_id))

    @staticmethod
    def _ingest_line(db, job_id, line, row_no, manifest, scopes, supplier_namespace, key_version):
        if not line:
            raise InputRejected("INPUT_ROW_MISSING")
        try:
            ticket = MultiEventTicketInput.model_validate_json(line)
        except Exception as exc:
            raise InputRejected("INPUT_ROW_INVALID") from exc
        scope_id = scopes.get(ticket.scope_key)
        if scope_id is None:
            raise InputRejected("INPUT_SCOPE_MISMATCH")
        if ticket.source_row_ordinal is None:
            raise InputRejected("SOURCE_ROW_ORDINAL_REQUIRED")
        if (ticket.supplier_namespace != manifest.supplier_namespace
                or ticket.hmac_purpose != manifest.hmac_purpose
                or ticket.hmac_key_version != manifest.hmac_key_version):
            raise InputRejected("IDENTITY_CONTRACT_MISMATCH")
        scope = manifest.ledger_scopes[scope_id]
        if ticket.quantity_status == "COMPLETE" and ticket.quantity_policy_snapshot_id != scope.policy_snapshot_id:
            raise InputRejected("QUANTITY_SNAPSHOT_SCOPE_MISMATCH")
        if supplier_namespace is not None and ticket.supplier_namespace != supplier_namespace:
            raise InputRejected("SUPPLIER_NAMESPACE_MIXED")
        if key_version is not None and ticket.hmac_key_version != key_version:
            raise InputRejected("HMAC_KEY_VERSION_MIXED")
        supplier_namespace = ticket.supplier_namespace
        key_version = ticket.hmac_key_version
        row_no += 1
        if row_no > manifest.record_count:
            raise InputRejected("RECORD_COUNT_EXCEEDED")
        try:
            db.execute("INSERT INTO mei_artifact_rows(job_id,scope_id,row_ordinal,account_id,reservation_no) VALUES(?,?,?,?,?)",
                       (job_id, scope_id, ticket.source_row_ordinal, ticket.account_id, ticket.reservation_no))
            db.execute("INSERT OR IGNORE INTO mei_artifact_accounts(job_id,scope_id,account_id) VALUES(?,?,?)",
                       (job_id, scope_id, ticket.account_id))
            for signal in BASE_SIGNALS:
                token = getattr(ticket, signal)
                if token is not None:
                    db.execute("INSERT OR IGNORE INTO mei_artifact_token_owners(job_id,scope_id,signal,token,account_id) VALUES(?,?,?,?,?)",
                               (job_id, scope_id, signal, token, ticket.account_id))
            window = ip_window_token(ticket)
            if window is not None:
                network_type = ticket.ip_network_type or "UNKNOWN"
                db.execute("INSERT OR IGNORE INTO mei_artifact_ip_types(job_id,scope_id,token,account_id,network_type) VALUES(?,?,?,?,?)",
                           (job_id, scope_id, window, ticket.account_id, network_type))
            db.execute("INSERT INTO mei_artifact_quantities(job_id,scope_id,account_id,row_ordinal,quantity,purchase_limit,snapshot_id,policy_version,source_version,row_source_version,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, scope_id, ticket.account_id, ticket.source_row_ordinal,
                        ticket.effective_ticket_quantity, ticket.confirmed_purchase_limit,
                        ticket.quantity_policy_snapshot_id, ticket.quantity_policy_version,
                        ticket.quantity_source_version, ticket.source_version, ticket.quantity_status, ticket.quantity_reason))
        except sqlite3.IntegrityError as exc:
            raise InputRejected("INPUT_ROW_DUPLICATE") from exc
        return row_no, supplier_namespace, key_version

    @staticmethod
    def _finalize_ip_windows(db, job_id: str) -> None:
        # A missing or conflicting network class becomes UNKNOWN for every owner
        # in the same fixed-scope 600-second IP window.
        rows = db.execute("""
            SELECT scope_id,token,CASE WHEN count(DISTINCT network_type)=1 THEN min(network_type) ELSE 'UNKNOWN' END network_type
            FROM mei_artifact_ip_types WHERE job_id=? GROUP BY scope_id,token ORDER BY scope_id,token
        """, (job_id,))
        for row in rows:
            signal = f"ip_window:{row['network_type']}"
            db.execute("""
                INSERT OR IGNORE INTO mei_artifact_token_owners(job_id,scope_id,signal,token,account_id)
                SELECT job_id,scope_id,?,token,account_id FROM mei_artifact_ip_types
                WHERE job_id=? AND scope_id=? AND token=?
            """, (signal, job_id, row["scope_id"], row["token"]))

    def account_page(self, job_id: str, scope_id: int, offset: int, limit: int) -> list[dict]:
        with _connect(self.path) as db:
            return [dict(row) for row in db.execute("""
                SELECT DISTINCT o.account_id FROM mei_artifact_token_owners o
                WHERE o.job_id=? AND o.scope_id=? AND o.projected=1
                ORDER BY o.account_id LIMIT ? OFFSET ?
            """, (job_id, scope_id, limit, offset))]

    def edge_page(self, job_id: str, scope_id: int, offset: int, limit: int) -> list[dict]:
        with _connect(self.path) as db:
            return [dict(row) for row in db.execute("""
                SELECT signal,token,account_id,weight FROM mei_artifact_token_owners
                WHERE job_id=? AND scope_id=? AND projected=1
                ORDER BY signal,token,account_id LIMIT ? OFFSET ?
            """, (job_id, scope_id, limit, offset))]

    def graph_scope_count(self, job_id: str, scope_id: int) -> tuple[int, int]:
        with _connect(self.path) as db:
            account_count = db.execute("SELECT count(DISTINCT account_id) FROM mei_artifact_token_owners WHERE job_id=? AND scope_id=? AND projected=1", (job_id, scope_id)).fetchone()[0]
            edge_count = db.execute("SELECT count(*) FROM mei_artifact_token_owners WHERE job_id=? AND scope_id=? AND projected=1", (job_id, scope_id)).fetchone()[0]
            return account_count, edge_count

    def graph_scope_token_count(self, job_id: str, scope_id: int) -> int:
        with _connect(self.path) as db:
            return db.execute("SELECT count(*) FROM (SELECT signal,token FROM mei_artifact_token_owners WHERE job_id=? AND scope_id=? AND projected=1 GROUP BY signal,token)", (job_id, scope_id)).fetchone()[0]

    def prepare_projected_tokens(self, job_id: str) -> int:
        projected_edges = 0
        with _connect(self.path) as db:
            db.execute("UPDATE mei_artifact_token_owners SET projected=0,weight=NULL WHERE job_id=?", (job_id,))
            scope_counts = {row["scope_id"]: row["n"] for row in db.execute("SELECT scope_id,count(*) n FROM mei_artifact_accounts WHERE job_id=? GROUP BY scope_id", (job_id,))}
            token_rows = db.execute("""
                SELECT scope_id,signal,token,count(DISTINCT account_id) n FROM mei_artifact_token_owners
                WHERE job_id=? GROUP BY scope_id,signal,token ORDER BY scope_id,signal,token
            """, (job_id,))
            while True:
                batch = token_rows.fetchmany(500)
                if not batch:
                    break
                for row in batch:
                    n, scope_count = row["n"], scope_counts.get(row["scope_id"], 0)
                    if n < 2:
                        continue
                    signal = row["signal"]
                    try:
                        factor = damping(signal, n, scope_count)
                    except ValueError as exc:
                        raise InputRejected("UNKNOWN_EXP2_SIGNAL") from exc
                    if factor is None:
                        continue
                    weight = DEFAULT_EXP2_PARAMETERS.reliability[signal] * factor
                    db.execute("UPDATE mei_artifact_token_owners SET projected=1,weight=? WHERE job_id=? AND scope_id=? AND signal=? AND token=?",
                               (weight, job_id, row["scope_id"], signal, row["token"]))
                    projected_edges += n
                    if projected_edges > MAX_PROJECTED_EDGES:
                        raise GdsResourceExhausted("PROJECTED_EDGE_LIMIT_EXCEEDED")
            return projected_edges

    def clear_communities(self, job_id: str, scope_id: int | None = None) -> None:
        with _connect(self.path) as db:
            if scope_id is None:
                db.execute("DELETE FROM mei_artifact_communities WHERE job_id=?", (job_id,))
            else:
                db.execute("DELETE FROM mei_artifact_communities WHERE job_id=? AND scope_id=?", (job_id, scope_id))

    def add_community_batch(self, job_id: str, scope_id: int, rows: list[tuple[str, int]]) -> None:
        with _connect(self.path) as db:
            db.executemany("INSERT INTO mei_artifact_communities(job_id,scope_id,account_id,community_id) VALUES(?,?,?,?)",
                           [(job_id, scope_id, account_id, community_id) for account_id, community_id in rows])

    def build_results(self, job_id: str, manifest: MultiEventArtifactManifest, algorithm_version: str) -> None:
        """Build recurrence candidates into indexed SQLite result pages.

        Only one bounded candidate membership set (at most 50,001 accounts) is
        materialized at a time. Evidence, candidates, members and relationships
        are otherwise joined and persisted in SQLite. 연결 조건과 점수 구성은 JSON 경로와 같은
        `services.multi_event_candidates` 순수 함수를 사용합니다.
        """
        scope_by_id = {index: scope for index, scope in enumerate(manifest.ledger_scopes)}
        with _connect(self.path) as db:
            self._build_quantity_contexts(db, job_id, scope_by_id)
            self._build_evidence_groups(db, job_id, scope_by_id)
            pair_count = db.execute("""
                SELECT count(*)
                FROM mei_artifact_evidence_groups l
                JOIN mei_artifact_evidence_groups r
                  ON r.job_id=l.job_id AND r.signal=l.signal AND r.token=l.token AND r.scope_id>l.scope_id
                JOIN mei_artifact_scopes ls ON ls.job_id=l.job_id AND ls.scope_id=l.scope_id
                JOIN mei_artifact_scopes rs ON rs.job_id=r.job_id AND rs.scope_id=r.scope_id
                WHERE l.job_id=? AND ls.performance_id<>rs.performance_id
            """, (job_id,)).fetchone()[0]
            if pair_count > MAX_RECURRENCE_COMPARISONS:
                raise MultiEventResourceLimit("RECURRENCE_COMPARISON_LIMIT_EXCEEDED")

            budget = {"candidates": 0, "member_rows": 0, "evidence_rows": 0}
            # (a) 강한 신호 반복: 같은 token이 서로 다른 회차의 같은 군집에서 반복된 정확한 교집합
            pairs = db.execute("""
                SELECT l.group_id left_group_id,r.group_id right_group_id,
                       l.scope_id left_scope_id,r.scope_id right_scope_id
                FROM mei_artifact_evidence_groups l
                JOIN mei_artifact_evidence_groups r
                  ON r.job_id=l.job_id AND r.signal=l.signal AND r.token=l.token AND r.scope_id>l.scope_id
                JOIN mei_artifact_scopes ls ON ls.job_id=l.job_id AND ls.scope_id=l.scope_id
                JOIN mei_artifact_scopes rs ON rs.job_id=r.job_id AND rs.scope_id=r.scope_id
                WHERE l.job_id=? AND ls.performance_id<>rs.performance_id
                ORDER BY l.signal,l.token,l.scope_id,r.scope_id,l.community_id,r.community_id
            """, (job_id,))
            comparisons = 0
            while True:
                batch = pairs.fetchmany(100)
                if not batch:
                    break
                for pair in batch:
                    comparisons += 1
                    left_scope = scope_by_id[pair["left_scope_id"]]
                    right_scope = scope_by_id[pair["right_scope_id"]]
                    if not recurrence_pair_allowed(left_scope.performance_id, right_scope.performance_id):
                        continue
                    owners = db.execute("""
                        SELECT l.account_id FROM mei_artifact_evidence_members l
                        JOIN mei_artifact_evidence_members r
                          ON r.job_id=l.job_id AND r.account_id=l.account_id
                        WHERE l.job_id=? AND l.group_id=? AND r.group_id=?
                        ORDER BY l.account_id LIMIT ?
                    """, (job_id, pair["left_group_id"], pair["right_group_id"], MAX_CANDIDATE_MEMBERS + 1)).fetchall()
                    if len(owners) < 2:
                        continue
                    candidate_id = self._register_candidate(db, job_id, manifest, owners, budget)
                    inserted = 0
                    for group_id in (pair["left_group_id"], pair["right_group_id"]):
                        inserted += db.execute("INSERT OR IGNORE INTO mei_artifact_candidate_evidence(job_id,candidate_id,group_id) VALUES(?,?,?)",
                                               (job_id, candidate_id, group_id)).rowcount
                    self._charge_evidence(db, job_id, candidate_id, inserted, budget)
                if comparisons % 1_000 == 0:
                    db.commit()

            # (b) 같은 계정의 반복 동시 군집: 계정 -> (회차, 군집) 역색인
            self._build_cocluster_candidates(db, job_id, manifest, scope_by_id, budget)

            candidate_cursor = db.execute("SELECT candidate_id FROM mei_artifact_candidates WHERE job_id=? ORDER BY candidate_id", (job_id,))
            completed_candidates = 0
            kept_candidates = 0
            relationship_member_rows = 0
            while True:
                candidate_batch = candidate_cursor.fetchmany(100)
                if not candidate_batch:
                    break
                for item in candidate_batch:
                    additional_relationship_members = self._finalize_candidate(
                        db, job_id, item["candidate_id"], manifest, scope_by_id,
                    )
                    if additional_relationship_members:
                        kept_candidates += 1
                    relationship_member_rows += additional_relationship_members
                    if relationship_member_rows > MAX_RELATIONSHIP_MEMBER_ROWS:
                        raise MultiEventResourceLimit("MULTI_EVENT_RELATIONSHIP_MEMBER_ROW_BUDGET_EXCEEDED")
                    completed_candidates += 1
                if completed_candidates % 100 == 0:
                    db.commit()
            if completed_candidates != budget["candidates"]:
                raise RuntimeError("MULTI_EVENT_CANDIDATE_RESULT_COUNT_MISMATCH")
            stored = db.execute("SELECT count(*) FROM mei_artifact_candidates WHERE job_id=?", (job_id,)).fetchone()[0]
            if stored != kept_candidates:
                raise RuntimeError("MULTI_EVENT_CANDIDATE_RESULT_COUNT_MISMATCH")
            changed = db.execute("UPDATE mei_artifact_jobs SET candidate_count=?,algorithm_version=?,updated_at=? WHERE job_id=? AND status='RUNNING'",
                                 (stored, algorithm_version, _now(), job_id)).rowcount
            if changed != 1:
                raise RuntimeError("MULTI_EVENT_JOB_STATE_CONFLICT")

    @staticmethod
    def _register_candidate(db, job_id: str, manifest, owners, budget: dict) -> str:
        """정확한 교집합 구성원으로 후보 자리를 만들거나 기존 후보를 재사용합니다."""
        if len(owners) > MAX_CANDIDATE_MEMBERS:
            raise MultiEventResourceLimit("CANDIDATE_MEMBER_LIMIT_EXCEEDED")
        accounts = [row["account_id"] for row in owners]
        candidate_id = _candidate_id(manifest.analysis_id, accounts)
        inserted = db.execute("""
            INSERT OR IGNORE INTO mei_artifact_candidates(
              job_id,candidate_id,priority_band,score,distinct_account_count,
              distinct_event_count,distinct_performance_count,verified_relationship_count,summary_json
            ) VALUES(?,?,'LOW',0,?,1,2,1,'{}')
        """, (job_id, candidate_id, len(accounts))).rowcount
        if inserted:
            budget["candidates"] += 1
            if budget["candidates"] > MAX_CANDIDATES:
                raise MultiEventResourceLimit("CANDIDATE_COUNT_LIMIT_EXCEEDED")
            if budget["member_rows"] + len(accounts) > MAX_CANDIDATE_MEMBER_ROWS:
                raise MultiEventResourceLimit("MULTI_EVENT_CANDIDATE_MEMBER_ROW_BUDGET_EXCEEDED")
            budget["member_rows"] += len(accounts)
            db.executemany("INSERT INTO mei_artifact_candidate_members(job_id,candidate_id,account_id) VALUES(?,?,?)",
                           [(job_id, candidate_id, account) for account in accounts])
        else:
            existing_count = db.execute("SELECT count(*) FROM mei_artifact_candidate_members WHERE job_id=? AND candidate_id=?", (job_id, candidate_id)).fetchone()[0]
            if existing_count != len(accounts):
                raise RuntimeError("MULTI_EVENT_CANDIDATE_ID_COLLISION")
        return candidate_id

    @staticmethod
    def _charge_evidence(db, job_id: str, candidate_id: str, inserted: int, budget: dict) -> None:
        budget["evidence_rows"] += inserted
        if budget["evidence_rows"] > MAX_CANDIDATE_EVIDENCE_ROWS:
            raise MultiEventResourceLimit("CANDIDATE_EVIDENCE_BUDGET_EXCEEDED")
        evidence_count = db.execute("""
            SELECT (SELECT count(*) FROM mei_artifact_candidate_evidence WHERE job_id=? AND candidate_id=?)
                 + (SELECT count(*) FROM mei_artifact_candidate_cocluster WHERE job_id=? AND candidate_id=?)
        """, (job_id, candidate_id, job_id, candidate_id)).fetchone()[0]
        if evidence_count > MAX_CANDIDATE_EVIDENCE_GROUPS:
            raise MultiEventResourceLimit("CANDIDATE_EVIDENCE_LIMIT_EXCEEDED")

    def _build_cocluster_candidates(self, db, job_id: str, manifest, scope_by_id, budget: dict) -> None:
        """두 회차에서 같은 GDS 군집에 함께 있었던 계정의 정확한 교집합을 후보로 만듭니다.

        비교 횟수는 계정별 소속 수로 먼저 계산하고, 상한을 넘으면 결과를 자르지 않고
        작업을 실패시킵니다. 추이 병합은 하지 않습니다.
        """
        multi_member = """
            SELECT c.account_id,c.scope_id,c.community_id FROM mei_artifact_communities c
            JOIN (SELECT scope_id,community_id FROM mei_artifact_communities WHERE job_id=?
                  GROUP BY scope_id,community_id HAVING count(*)>=2) g
              ON g.scope_id=c.scope_id AND g.community_id=c.community_id
            WHERE c.job_id=?
        """
        membership_counts = (row[0] for row in db.execute(
            f"SELECT count(*) FROM ({multi_member}) GROUP BY account_id", (job_id, job_id),
        ))
        try:
            require_comparison_budget(cocluster_comparison_count(membership_counts), MAX_RECURRENCE_COMPARISONS)
        except RecurrenceComparisonLimit as exc:
            raise MultiEventResourceLimit(exc.code) from None

        memberships = db.execute(f"{multi_member} ORDER BY c.account_id,c.scope_id", (job_id, job_id))
        current_account = None
        current: list[tuple[int, str, int]] = []
        staged = 0

        def flush() -> None:
            nonlocal staged
            if current_account is None:
                return
            rows = [
                (job_id, left[0], left[1], right[0], right[1], current_account)
                for left, right in cocluster_pairs(current)
            ]
            if rows:
                db.executemany("""
                    INSERT INTO mei_artifact_cocluster_members(
                      job_id,left_scope_id,left_community_id,right_scope_id,right_community_id,account_id
                    ) VALUES(?,?,?,?,?,?)
                """, rows)
                staged += len(rows)
                if staged % 5_000 < len(rows):
                    db.commit()

        while True:
            batch = memberships.fetchmany(500)
            if not batch:
                break
            for row in batch:
                if row["account_id"] != current_account:
                    flush()
                    current_account, current = row["account_id"], []
                scope_id = row["scope_id"]
                current.append((scope_id, scope_by_id[scope_id].performance_id, row["community_id"]))
        flush()

        groups = db.execute("""
            SELECT left_scope_id,left_community_id,right_scope_id,right_community_id
            FROM mei_artifact_cocluster_members WHERE job_id=?
            GROUP BY left_scope_id,left_community_id,right_scope_id,right_community_id
            HAVING count(*)>=2
            ORDER BY left_scope_id,left_community_id,right_scope_id,right_community_id
        """, (job_id,)).fetchall()
        for group in groups:
            owners = db.execute("""
                SELECT account_id FROM mei_artifact_cocluster_members
                WHERE job_id=? AND left_scope_id=? AND left_community_id=? AND right_scope_id=? AND right_community_id=?
                ORDER BY account_id LIMIT ?
            """, (job_id, group["left_scope_id"], group["left_community_id"], group["right_scope_id"],
                  group["right_community_id"], MAX_CANDIDATE_MEMBERS + 1)).fetchall()
            candidate_id = self._register_candidate(db, job_id, manifest, owners, budget)
            inserted = 0
            for scope_id in (group["left_scope_id"], group["right_scope_id"]):
                inserted += db.execute("INSERT OR IGNORE INTO mei_artifact_candidate_cocluster(job_id,candidate_id,scope_id) VALUES(?,?,?)",
                                       (job_id, candidate_id, scope_id)).rowcount
            self._charge_evidence(db, job_id, candidate_id, inserted, budget)

    @staticmethod
    def _build_quantity_contexts(db, job_id: str, scope_by_id) -> None:
        cursor = db.execute("""
            SELECT scope_id,account_id,quantity,purchase_limit,snapshot_id,policy_version,
                   source_version,row_source_version,status,reason
            FROM mei_artifact_quantities WHERE job_id=?
            ORDER BY scope_id,account_id,row_ordinal
        """, (job_id,))
        current_key = None
        provenance = None
        inconsistent = False
        reasons: set[str] = set()

        def add_reason(value: str) -> None:
            value = value[:512]
            if value in reasons:
                return
            if len(reasons) < MAX_QUANTITY_REASONS:
                reasons.add(value)
            else:
                reasons.add("MULTIPLE_QUANTITY_UNCERTAINTY_REASONS")

        def save_context(key):
            nonlocal provenance, inconsistent, reasons
            if key is None:
                return
            scope_id, account_id = key
            if reasons or inconsistent or provenance is None:
                if inconsistent:
                    add_reason("QUANTITY_ACCOUNT_OBSERVATION_INCONSISTENT")
                if not reasons:
                    add_reason("QUANTITY_SOURCE_UNAVAILABLE")
                value, status = DEFAULT_EXP2_PARAMETERS.quantity_neutral, "NEUTRAL_NO_POLICY"
                ordered = sorted(reasons)
                reason = "|".join(ordered)
                if len(reason) > 512:
                    reason = "MULTIPLE_QUANTITY_UNCERTAINTY_REASONS"
            else:
                quantity_value, limit, *_ = provenance
                value, status, reason = min(quantity_value / limit, 1.0), "VERIFIED", "POLICY_SNAPSHOT_AND_QUANTITY_VERIFIED"
            db.execute("INSERT INTO mei_artifact_quantity_contexts(job_id,scope_id,account_id,value,status,reason) VALUES(?,?,?,?,?,?)",
                       (job_id, scope_id, account_id, value, status, reason))
            provenance = None
            inconsistent = False
            reasons = set()

        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                key = row["scope_id"], row["account_id"]
                if current_key is not None and key != current_key:
                    save_context(current_key)
                current_key = key
                if row["status"] != "COMPLETE":
                    add_reason(row["reason"] or row["status"] or "QUANTITY_INCOMPLETE")
                    continue
                scope = scope_by_id[row["scope_id"]]
                if (scope.policy_snapshot_id is None
                        or row["snapshot_id"] != scope.policy_snapshot_id
                        or row["source_version"] != row["row_source_version"]
                        or row["quantity"] is None or row["purchase_limit"] is None
                        or row["policy_version"] is None):
                    add_reason("QUANTITY_POLICY_SOURCE_MISMATCH")
                    continue
                value = (row["quantity"], row["purchase_limit"], row["snapshot_id"],
                         row["policy_version"], row["source_version"])
                if provenance is not None and value != provenance:
                    inconsistent = True
                else:
                    provenance = value
        save_context(current_key)

    @staticmethod
    def _build_evidence_groups(db, job_id: str, scope_by_id) -> None:
        scope_counts = {
            row["scope_id"]: row["account_count"]
            for row in db.execute("""
                SELECT s.scope_id,count(a.account_id) account_count
                FROM mei_artifact_scopes s LEFT JOIN mei_artifact_accounts a
                  ON a.job_id=s.job_id AND a.scope_id=s.scope_id
                WHERE s.job_id=? GROUP BY s.scope_id
            """, (job_id,))
        }
        signals = tuple(sorted(STRONG_RECURRENCE_SIGNALS))
        marks = ",".join("?" for _ in signals)
        groups = db.execute(f"""
            SELECT scope_id,signal,token,count(*) owner_count
            FROM mei_artifact_token_owners
            WHERE job_id=? AND signal IN ({marks})
            GROUP BY scope_id,signal,token ORDER BY scope_id,signal,token
        """, (job_id, *signals))
        evidence_group_count = 0
        while True:
            batch = groups.fetchmany(500)
            if not batch:
                break
            for token_group in batch:
                if token_group["owner_count"] < 2:
                    continue
                factor = token_damping(token_group["signal"], token_group["owner_count"], scope_counts[token_group["scope_id"]])
                if factor is None:
                    # 제외된 hub token은 연결 근거가 아닙니다. 후보의 제외 신호는 점수 구성 단계에서
                    # 구성원이 실제로 공유한 token 기준으로 다시 판정합니다.
                    continue

                community_groups = db.execute("""
                    SELECT c.community_id,count(*) owner_count
                    FROM mei_artifact_token_owners o
                    JOIN mei_artifact_communities c
                      ON c.job_id=o.job_id AND c.scope_id=o.scope_id AND c.account_id=o.account_id
                    WHERE o.job_id=? AND o.scope_id=? AND o.signal=? AND o.token=?
                    GROUP BY c.community_id HAVING count(*)>=2 ORDER BY c.community_id
                """, (job_id, token_group["scope_id"], token_group["signal"], token_group["token"]))
                while True:
                    community_batch = community_groups.fetchmany(500)
                    if not community_batch:
                        break
                    for community in community_batch:
                        group_source = f"{job_id}|{token_group['scope_id']}|{token_group['signal']}|{token_group['token']}|{community['community_id']}"
                        group_id = hashlib.sha256(group_source.encode("utf-8")).hexdigest()
                        inserted = db.execute("""
                            INSERT OR IGNORE INTO mei_artifact_evidence_groups(
                              job_id,group_id,scope_id,signal,token,community_id,owner_count,damping
                            ) VALUES(?,?,?,?,?,?,?,?)
                        """, (job_id, group_id, token_group["scope_id"], token_group["signal"], token_group["token"], community["community_id"], community["owner_count"], factor)).rowcount
                        if inserted:
                            evidence_group_count += 1
                            if evidence_group_count > MAX_PROJECTED_EDGES:
                                raise MultiEventResourceLimit("EVIDENCE_GROUP_LIMIT_EXCEEDED")
                            db.execute("""
                                INSERT INTO mei_artifact_evidence_members(job_id,group_id,account_id)
                                SELECT o.job_id,?,o.account_id FROM mei_artifact_token_owners o
                                JOIN mei_artifact_communities c
                                  ON c.job_id=o.job_id AND c.scope_id=o.scope_id AND c.account_id=o.account_id
                                WHERE o.job_id=? AND o.scope_id=? AND o.signal=? AND o.token=? AND c.community_id=?
                            """, (group_id, job_id, token_group["scope_id"], token_group["signal"], token_group["token"], community["community_id"]))
            if evidence_group_count % 1_000 == 0:
                db.commit()

    @staticmethod
    def _finalize_candidate(db, job_id, candidate_id, manifest, scope_by_id) -> int:
        member_count = db.execute("SELECT count(*) FROM mei_artifact_candidate_members WHERE job_id=? AND candidate_id=?", (job_id, candidate_id)).fetchone()[0]
        if member_count < 2 or member_count > MAX_CANDIDATE_MEMBERS:
            raise MultiEventResourceLimit("CANDIDATE_MEMBER_LIMIT_EXCEEDED")
        relation_rows = db.execute("""
            SELECT DISTINCT g.signal relation_type,g.scope_id FROM mei_artifact_candidate_evidence ce
            JOIN mei_artifact_evidence_groups g ON g.job_id=ce.job_id AND g.group_id=ce.group_id
            WHERE ce.job_id=? AND ce.candidate_id=?
            UNION
            SELECT ?,scope_id FROM mei_artifact_candidate_cocluster WHERE job_id=? AND candidate_id=?
            ORDER BY 1,2
        """, (job_id, candidate_id, STABLE_ACCOUNT_COCLUSTER, job_id, candidate_id)).fetchall()
        if len(relation_rows) < 2 or len(relation_rows) > MAX_CANDIDATE_EVIDENCE_GROUPS:
            raise MultiEventResourceLimit("CANDIDATE_EVIDENCE_LIMIT_EXCEEDED")
        scopes_by_type: dict[str, list[int]] = {}
        for row in relation_rows:
            scopes_by_type.setdefault(row["relation_type"], []).append(row["scope_id"])
        scope_ids = sorted({row["scope_id"] for row in relation_rows})
        source_scopes = sorted((_public_scope(scope_by_id[index]) for index in scope_ids), key=lambda item: item.key)
        db.executemany("INSERT OR IGNORE INTO mei_artifact_candidate_scopes(job_id,candidate_id,scope_id) VALUES(?,?,?)",
                       [(job_id, candidate_id, index) for index in scope_ids])

        # 점수 구성: 출처 회차에서 구성원 두 명 이상이 실제로 공유한 모든 exp2 신호
        marks = ",".join("?" for _ in scope_ids)
        scope_counts = {row["scope_id"]: row["n"] for row in db.execute(
            f"SELECT scope_id,count(*) n FROM mei_artifact_accounts WHERE job_id=? AND scope_id IN ({marks}) GROUP BY scope_id",
            (job_id, *scope_ids),
        )}
        member_tokens = """
            SELECT o.scope_id,o.signal,o.token,o.account_id
            FROM mei_artifact_candidate_members cm
            JOIN mei_artifact_candidate_scopes cs ON cs.job_id=cm.job_id AND cs.candidate_id=cm.candidate_id
            JOIN mei_artifact_token_owners o ON o.job_id=cm.job_id AND o.account_id=cm.account_id AND o.scope_id=cs.scope_id
            WHERE cm.job_id=? AND cm.candidate_id=?
        """
        db.execute("CREATE TEMP TABLE IF NOT EXISTS mei_included_tokens(scope_id INTEGER NOT NULL, signal TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY(scope_id,signal,token))")
        db.execute("DELETE FROM mei_included_tokens")
        dampings: dict[str, list[float]] = {}
        excluded: set[str] = set()
        shared = db.execute(f"""
            SELECT mt.scope_id,mt.signal,mt.token,
              (SELECT count(*) FROM mei_artifact_token_owners t
               WHERE t.job_id=? AND t.scope_id=mt.scope_id AND t.signal=mt.signal AND t.token=mt.token) owner_count
            FROM ({member_tokens}) mt
            GROUP BY mt.scope_id,mt.signal,mt.token HAVING count(*)>=2
            ORDER BY mt.signal,mt.scope_id,mt.token
        """, (job_id, job_id, candidate_id)).fetchall()
        for row in shared:
            try:
                factor = token_damping(row["signal"], row["owner_count"], scope_counts.get(row["scope_id"], 0))
            except ValueError as exc:
                raise InputRejected("UNKNOWN_EXP2_SIGNAL") from exc
            if factor is None:
                excluded.add(row["signal"])
                continue
            dampings.setdefault(row["signal"], []).append(factor)
            db.execute("INSERT INTO mei_included_tokens(scope_id,signal,token) VALUES(?,?,?)",
                       (row["scope_id"], row["signal"], row["token"]))
        covered = {row["signal"]: row["covered"] for row in db.execute(f"""
            SELECT mt.signal,count(DISTINCT mt.account_id) covered FROM ({member_tokens}) mt
            JOIN mei_included_tokens i ON i.scope_id=mt.scope_id AND i.signal=mt.signal AND i.token=mt.token
            GROUP BY mt.signal ORDER BY mt.signal
        """, (job_id, candidate_id))}
        db.execute("DELETE FROM mei_included_tokens")
        link = link_inputs(member_count, covered, dampings, excluded)

        q_row = db.execute("""
            SELECT count(*) expected_contexts,
                   sum(CASE WHEN q.account_id IS NOT NULL THEN 1 ELSE 0 END) present_contexts,
                   sum(CASE WHEN q.status='VERIFIED' THEN 1 ELSE 0 END) verified_contexts,
                   avg(CASE WHEN q.status='VERIFIED' THEN q.value END) mean_quantity
            FROM mei_artifact_candidate_members cm
            CROSS JOIN mei_artifact_candidate_scopes cs
            LEFT JOIN mei_artifact_quantity_contexts q
              ON q.job_id=cm.job_id AND q.scope_id=cs.scope_id AND q.account_id=cm.account_id
            WHERE cm.job_id=? AND cm.candidate_id=? AND cs.candidate_id=?
        """, (job_id, candidate_id, candidate_id)).fetchone()
        if (q_row["expected_contexts"] > 0
                and q_row["present_contexts"] == q_row["expected_contexts"]
                and q_row["verified_contexts"] == q_row["expected_contexts"]):
            quantity = quantity_context(q_row["mean_quantity"], "POLICY_SNAPSHOT_AND_QUANTITY_VERIFIED")
        else:
            reasons = set()
            if q_row["present_contexts"] < q_row["expected_contexts"]:
                reasons.add("QUANTITY_SOURCE_SCOPE_MISSING")
            q_reasons = db.execute("""
                SELECT DISTINCT q.reason FROM mei_artifact_candidate_members cm
                JOIN mei_artifact_candidate_scopes cs ON cs.job_id=cm.job_id AND cs.candidate_id=cm.candidate_id
                JOIN mei_artifact_quantity_contexts q ON q.job_id=cm.job_id AND q.scope_id=cs.scope_id AND q.account_id=cm.account_id
                WHERE cm.job_id=? AND cm.candidate_id=? AND q.status<>'VERIFIED'
                ORDER BY q.reason LIMIT ?
            """, (job_id, candidate_id, MAX_QUANTITY_REASONS + 1))
            for row in q_reasons:
                if row["reason"]:
                    reasons.add(row["reason"])
            if len(reasons) > MAX_QUANTITY_REASONS:
                reasons = {"MULTIPLE_QUANTITY_UNCERTAINTY_REASONS"}
            quantity_reason = "|".join(sorted(reasons)) or "QUANTITY_SOURCE_UNAVAILABLE"
            if len(quantity_reason) > 512:
                quantity_reason = "MULTIPLE_QUANTITY_UNCERTAINTY_REASONS"
            quantity = quantity_context(None, quantity_reason)

        evaluation = evaluate_candidate(
            member_count, [(scope.event_id, scope.performance_id) for scope in source_scopes], link, quantity,
        )
        if evaluation is None:
            db.execute("DELETE FROM mei_artifact_candidates WHERE job_id=? AND candidate_id=?", (job_id, candidate_id))
            return 0
        score = evaluation.score

        for relation_type, relation_scope_ids in sorted(scopes_by_type.items()):
            relation_scopes = sorted((_public_scope(scope_by_id[index]) for index in relation_scope_ids), key=lambda item: item.key)
            rel_id = "rel-" + hashlib.sha256(f"MEI_REL|{candidate_id}|{relation_type}".encode("utf-8")).hexdigest()
            scope_json = json.dumps([scope.model_dump(mode="json", by_alias=True) for scope in relation_scopes], sort_keys=True, separators=(",", ":"))
            db.execute("INSERT OR REPLACE INTO mei_artifact_relationships(job_id,candidate_id,relationship_id,signal_type,account_count,source_scope_count,source_scopes_json) VALUES(?,?,?,?,?,?,?)",
                       (job_id, candidate_id, rel_id, relation_type, member_count, len(relation_scopes), scope_json))

        summary = MultiEventCandidateSummary(
            candidateId=candidate_id,
            priorityBand=evaluation.priority_band,
            scoreVersion=manifest.score_version,
            projectionVersion=manifest.projection_version,
            distinctAccountCount=member_count,
            distinctEventCount=evaluation.distinct_event_count,
            distinctPerformanceCount=evaluation.distinct_performance_count,
            verifiedRelationshipCount=len(relation_rows),
            sourceScopes=source_scopes,
            availableSignals=list(evaluation.available_signals),
            excludedSignals=list(evaluation.excluded_signals),
            unavailableSignals=list(evaluation.unavailable_signals),
            normalSharingPossibilities=list(evaluation.normal_sharing_possibilities),
            scoreBreakdown=MultiEventScoreBreakdown(
                L=score.link, G=score.size, B=score.behavior, Q=score.quantity,
                R=score.recurrence, score=score.score,
                quantityStatus=score.quantity_status, quantityReason=score.quantity_reason,
            ),
        )
        db.execute("""
            UPDATE mei_artifact_candidates SET priority_band=?,score=?,distinct_account_count=?,
              distinct_event_count=?,distinct_performance_count=?,verified_relationship_count=?,summary_json=?
            WHERE job_id=? AND candidate_id=?
        """, (summary.priority_band, score.score, summary.distinct_account_count,
              summary.distinct_event_count, summary.distinct_performance_count,
              summary.verified_relationship_count, summary.model_dump_json(by_alias=True), job_id, candidate_id))
        return member_count * len(scopes_by_type)

    def _completed_job_manifest(self, db, job_id: str):
        row = db.execute("SELECT status,manifest_json FROM mei_artifact_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail={"code": "MULTI_EVENT_JOB_NOT_FOUND"})
        if row["status"] != "COMPLETED":
            raise HTTPException(409, detail={"code": "RESULT_NOT_AVAILABLE", "status": row["status"]})
        return MultiEventArtifactManifest.model_validate_json(row["manifest_json"], context=STORED_CONTEXT)

    @staticmethod
    def _cursor_values(cursor: str | None, limit: int) -> tuple[int, int]:
        if type(limit) is not int or not 1 <= limit <= PAGE_SIZE_MAX:
            raise HTTPException(422, detail={"code": "INVALID_PAGE_LIMIT"})
        try:
            offset = 0 if cursor is None else int(cursor)
        except (TypeError, ValueError):
            raise HTTPException(422, detail={"code": "INVALID_CURSOR"}) from None
        if offset < 0:
            raise HTTPException(422, detail={"code": "INVALID_CURSOR"})
        return offset, limit

    def candidate_page(self, job_id: str, cursor: str | None, limit: int) -> MultiEventCandidatePage:
        offset, limit = self._cursor_values(cursor, limit)
        with _connect(self.path) as db:
            manifest = self._completed_job_manifest(db, job_id)
            total = db.execute("SELECT count(*) FROM mei_artifact_candidates WHERE job_id=?", (job_id,)).fetchone()[0]
            if offset > total:
                raise HTTPException(422, detail={"code": "INVALID_CURSOR"})
            rows = db.execute("""
                SELECT summary_json FROM mei_artifact_candidates
                WHERE job_id=? ORDER BY score DESC,candidate_id LIMIT ? OFFSET ?
            """, (job_id, limit, offset)).fetchall()
        records = [MultiEventCandidateSummary.model_validate_json(row["summary_json"]) for row in rows]
        next_cursor = str(offset + len(records)) if offset + len(records) < total else None
        return MultiEventCandidatePage(
            jobId=job_id, ledgerVersionSetDigest=manifest.ledger_version_set_digest,
            cursor=cursor, nextCursor=next_cursor, totalCount=total, records=records,
        )

    def member_page(self, job_id: str, candidate_id: str, cursor: str | None, limit: int) -> MultiEventMemberPage:
        offset, limit = self._cursor_values(cursor, limit)
        if not candidate_id or not candidate_id.startswith("multi-"):
            raise HTTPException(404, detail={"code": "CANDIDATE_NOT_FOUND"})
        with _connect(self.path) as db:
            manifest = self._completed_job_manifest(db, job_id)
            count = db.execute("SELECT distinct_account_count FROM mei_artifact_candidates WHERE job_id=? AND candidate_id=?", (job_id, candidate_id)).fetchone()
            if count is None:
                raise HTTPException(404, detail={"code": "CANDIDATE_NOT_FOUND"})
            total = count["distinct_account_count"]
            if offset > total:
                raise HTTPException(422, detail={"code": "INVALID_CURSOR"})
            rows = db.execute("""
                SELECT account_id FROM mei_artifact_candidate_members
                WHERE job_id=? AND candidate_id=? ORDER BY account_id LIMIT ? OFFSET ?
            """, (job_id, candidate_id, limit, offset)).fetchall()
        records = [row["account_id"] for row in rows]
        next_cursor = str(offset + len(records)) if offset + len(records) < total else None
        return MultiEventMemberPage(
            jobId=job_id, candidateId=candidate_id, ledgerVersionSetDigest=manifest.ledger_version_set_digest,
            cursor=cursor, nextCursor=next_cursor, totalCount=total, records=records,
        )

    def relationship_page(self, job_id: str, candidate_id: str, cursor: str | None, limit: int) -> MultiEventRelationshipPage:
        offset, limit = self._cursor_values(cursor, limit)
        if not candidate_id or not candidate_id.startswith("multi-"):
            raise HTTPException(404, detail={"code": "CANDIDATE_NOT_FOUND"})
        with _connect(self.path) as db:
            manifest = self._completed_job_manifest(db, job_id)
            found = db.execute("SELECT 1 FROM mei_artifact_candidates WHERE job_id=? AND candidate_id=?", (job_id, candidate_id)).fetchone()
            if not found:
                raise HTTPException(404, detail={"code": "CANDIDATE_NOT_FOUND"})
            total = db.execute("SELECT count(*) FROM mei_artifact_relationships WHERE job_id=? AND candidate_id=?", (job_id, candidate_id)).fetchone()[0]
            if offset > total:
                raise HTTPException(422, detail={"code": "INVALID_CURSOR"})
            rows = db.execute("""
                SELECT relationship_id,signal_type,account_count,source_scope_count,source_scopes_json
                FROM mei_artifact_relationships WHERE job_id=? AND candidate_id=?
                ORDER BY signal_type,relationship_id LIMIT ? OFFSET ?
            """, (job_id, candidate_id, limit, offset)).fetchall()
        records = [MultiEventRelationshipSummary(
            relationshipId=row["relationship_id"], signalType=row["signal_type"],
            accountCount=row["account_count"], sourceScopeCount=row["source_scope_count"],
            sourceScopes=json.loads(row["source_scopes_json"]),
        ) for row in rows]
        next_cursor = str(offset + len(records)) if offset + len(records) < total else None
        return MultiEventRelationshipPage(
            jobId=job_id, candidateId=candidate_id, ledgerVersionSetDigest=manifest.ledger_version_set_digest,
            cursor=cursor, nextCursor=next_cursor, totalCount=total, records=records,
        )

    def scope_rows(self, job_id: str) -> list[dict]:
        with _connect(self.path) as db:
            return [dict(row) for row in db.execute("SELECT scope_id,event_id,performance_id,ledger_version_id,scope_json FROM mei_artifact_scopes WHERE job_id=? ORDER BY scope_id", (job_id,))]

    def cancelled(self, job_id: str) -> bool:
        return False


class MultiEventArtifactJobService:
    """Schedules artifact ingest and analysis without tying up the request thread."""

    def __init__(self, repository: MultiEventArtifactJobRepository, store_factory, graph_service_factory):
        self.repository = repository
        self.store_factory = store_factory
        self.graph_service_factory = graph_service_factory
        self.tasks: dict[str, asyncio.Task] = {}
        try:
            max_concurrent = int(os.getenv("MULTI_EVENT_MAX_CONCURRENT_JOBS", "1"))
        except ValueError as exc:
            raise RuntimeError("MULTI_EVENT_MAX_CONCURRENT_JOBS must be an integer") from exc
        if not 1 <= max_concurrent <= 4:
            raise RuntimeError("MULTI_EVENT_MAX_CONCURRENT_JOBS must be between 1 and 4")
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def submit(self, manifest: MultiEventArtifactManifest, idempotency_key: str) -> MultiEventArtifactJobAccepted:
        accepted = self.repository.create(idempotency_key, manifest)
        if accepted.status == "QUEUED" and accepted.job_id not in self.tasks:
            self.tasks[accepted.job_id] = asyncio.create_task(self._run(accepted.job_id))
        return accepted

    def retry(self, job_id: str) -> MultiEventArtifactJobAccepted:
        if job_id in self.tasks:
            raise HTTPException(409, detail={"code": "MULTI_EVENT_JOB_ALREADY_RUNNING"})
        accepted = self.repository.retry(job_id)
        self.tasks[job_id] = asyncio.create_task(self._run(job_id))
        return accepted

    def resume_queued(self) -> None:
        """Redispatch durable QUEUED jobs during single-process service startup."""
        for job_id in self.repository.queued_job_ids():
            if job_id not in self.tasks:
                self.tasks[job_id] = asyncio.create_task(self._run(job_id))

    async def _run(self, job_id: str) -> None:
        # Acquire capacity before the atomic QUEUED -> RUNNING claim so queued
        # jobs remain visibly queued until a GDS slot is available.
        try:
            async with self._semaphore:
                await self._run_claimed(job_id)
        finally:
            self.tasks.pop(job_id, None)

    async def _run_claimed(self, job_id: str) -> None:
        manifest = self.repository.manifest(job_id)
        if not self.repository.start(job_id):
            return
        if manifest.score_version in RETIRED_SCORE_VERSIONS:
            # 퇴역 점수 버전으로 저장된 작업은 결과 조회만 허용하고 새 규칙으로 다시 계산하지 않습니다.
            self.repository.transition(job_id, "INPUT_REJECTED", failure="SCORE_VERSION_RETIRED")
            self.tasks.pop(job_id, None)
            return
        try:
            await asyncio.to_thread(self.repository.clear_stage, job_id)
            store = self.store_factory() if callable(self.store_factory) else self.store_factory
            graph_service = self.graph_service_factory() if callable(self.graph_service_factory) else self.graph_service_factory
            if inspect.isawaitable(graph_service):
                graph_service = await graph_service
            await asyncio.to_thread(
                self.repository.ingest, job_id,
                store.iter_bytes(manifest.object_key, manifest.object_version), manifest,
            )
            algorithm = await graph_service.analyze_multi_event_artifact(self.repository, job_id, manifest)
            await asyncio.to_thread(self.repository.build_results, job_id, manifest, algorithm)
            self.repository.transition(job_id, "COMPLETED", algorithm=algorithm)
        except ObjectVersionMismatch:
            self.repository.transition(job_id, "INPUT_REJECTED", failure="OBJECT_VERSION_MISMATCH")
        except InputRejected as exc:
            self.repository.transition(job_id, "INPUT_REJECTED", failure=getattr(exc, "code", "INPUT_REJECTED"))
        except GdsResourceExhausted as exc:
            self.repository.transition(job_id, "FAILED", failure=getattr(exc, "code", "GDS_RESOURCE_EXHAUSTED"))
        except Exception as exc:
            code = "GDS_UNAVAILABLE" if type(exc).__name__ == "GraphAnalysisUnavailable" else "MULTI_EVENT_ANALYSIS_FAILED"
            # Exception text can contain submitted data; record only the class.
            logger.error("Multi-event artifact worker failed (error_type=%s)", type(exc).__name__)
            self.repository.transition(job_id, "FAILED", failure=code)
        finally:
            self.tasks.pop(job_id, None)
