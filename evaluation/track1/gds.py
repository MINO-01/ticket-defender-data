"""Optional B3 adapter for the existing isolated GDS Louvain implementation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse
from uuid import uuid4

from .models import BaselineOutput, EvaluationRecord


_ISOLATED_PROJECT_PATTERN = re.compile(r"^tdissue19gds[0-9a-f]{10}$")
_EXPECTED_GDS_JAR = "td-issue19-gds-2.6.6.jar"


def _docker_output(arguments: list[str]) -> str | None:
    """Read Docker metadata without starting, stopping, or modifying containers."""

    try:
        completed = subprocess.run(
            ["docker", *arguments], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _has_expected_read_only_jar_mount(container: dict) -> bool:
    """Require the exact read-only GDS test JAR bind mount used by the test script."""

    for mount in container.get("Mounts", []):
        source = mount.get("Source", "")
        if (
            mount.get("Type") == "bind"
            and mount.get("Destination") == "/plugins/graph-data-science.jar"
            and mount.get("RW") is False
            and Path(source).name == _EXPECTED_GDS_JAR
        ):
            return True
    return False


def verify_isolated_gds_target(uri: str) -> str | None:
    """Return a safe-to-disclose failure code unless URI maps to the test Compose container.

    The accepted target is deliberately narrower than a loopback URI: it must be
    the dynamically mapped Bolt port of the read-only GDS container created by
    ``tests/run_gds_integration.ps1``. This runs before importing or creating
    ``GraphService`` and therefore before any Neo4j connection is attempted.
    """

    try:
        parsed = urlparse(uri)
        port = parsed.port
    except ValueError:
        return "INVALID_URI"
    if parsed.scheme not in {"bolt", "neo4j"} or parsed.hostname != "127.0.0.1":
        return "NON_ISOLATED_HOST"
    if port is None or port == 7687:
        return "NON_ISOLATED_PORT"

    container_ids = _docker_output([
        "container", "ls", "--quiet", "--filter", "label=com.docker.compose.service=neo4j",
    ])
    if not container_ids:
        return "NO_VERIFIED_GDS_CONTAINER"
    raw_inspect = _docker_output(["container", "inspect", *container_ids.split()])
    if not raw_inspect:
        return "NO_VERIFIED_GDS_CONTAINER"
    try:
        containers = json.loads(raw_inspect)
    except json.JSONDecodeError:
        return "NO_VERIFIED_GDS_CONTAINER"

    verified = []
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {}) or {}
        project = labels.get("com.docker.compose.project", "")
        if (
            labels.get("com.docker.compose.service") != "neo4j"
            or not _ISOLATED_PROJECT_PATTERN.fullmatch(project)
            or container.get("Config", {}).get("Image") != "neo4j:5.12.0"
            or not _has_expected_read_only_jar_mount(container)
        ):
            continue
        mappings = container.get("NetworkSettings", {}).get("Ports", {}).get("7687/tcp") or []
        if any(mapping.get("HostIp") == "127.0.0.1" and mapping.get("HostPort") == str(port) for mapping in mappings):
            verified.append(container)
    return None if len(verified) == 1 else "NO_VERIFIED_GDS_CONTAINER"


async def _run_existing_louvain(
    records: tuple[EvaluationRecord, ...], uri: str, password: str
) -> BaselineOutput:
    # Keep B0--B2 and fixture tests independent from optional Neo4j/GDS packages.
    # The existing production implementation is imported only for an explicit B3 run.
    from schemas.dto import Track1AnalysisRequest
    from services.graph_service import GraphService

    scores = {record.ticket_id: 0.0 for record in records}
    labels = {record.ticket_id: f"singleton-{record.ticket_id}" for record in records}
    eligible = [record for record in records if record.has_signal]
    if not eligible:
        return BaselineOutput("B3_GDS_LOUVAIN", "COMPLETED", scores, labels, "No records had observable signals.")

    request = Track1AnalysisRequest.model_validate({
        "requestId": str(uuid4()),
        "tickets": [
            {
                "accountId": record.account_id,
                "reservationNo": record.reservation_no,
                "eventId": record.event_id,
                "paymentHash": record.signals.get("payment_hash"),
                "addressHash": record.signals.get("address_hash"),
                "deviceIdHash": record.signals.get("device_id_hash"),
                "ipHash": record.signals.get("ip_hash"),
            }
            for record in eligible
        ],
    })
    service = GraphService(uri, "neo4j", password)
    try:
        response = await service.analyze_louvain(request)
    finally:
        await service.close()

    by_key = {record.key: record.ticket_id for record in records}
    for cluster in response.clusters:
        for member in cluster.members:
            ticket_id = by_key[(member.account_id, member.reservation_no, member.event_id)]
            scores[ticket_id] = max(scores[ticket_id], cluster.risk_score)
            labels[ticket_id] = cluster.cluster_id
    return BaselineOutput(
        name="B3_GDS_LOUVAIN",
        status="COMPLETED",
        scores=scores,
        cluster_labels=labels,
        algorithm_version=response.algorithm_version,
    )


def b3_existing_gds_louvain(
    records: tuple[EvaluationRecord, ...], *, uri: str | None, password: str | None
) -> BaselineOutput:
    """Run only against a caller-provided isolated GDS instance.

    The adapter never starts Docker or contacts a configured application Neo4j.
    Missing isolation credentials are a deliberate skip, not a synthetic result.
    """

    if not uri or not password:
        return BaselineOutput(
            name="B3_GDS_LOUVAIN",
            status="SKIPPED_GDS_ISOLATION",
            scores={record.ticket_id: 0.0 for record in records},
            detail="Set an isolated GDS URI and password to run B3.",
        )
    isolation_failure = verify_isolated_gds_target(uri)
    if isolation_failure is not None:
        return BaselineOutput(
            name="B3_GDS_LOUVAIN",
            status="SKIPPED_GDS_ISOLATION",
            scores={record.ticket_id: 0.0 for record in records},
            detail=f"GDS isolation verification failed: {isolation_failure}",
        )
    try:
        return asyncio.run(_run_existing_louvain(records, uri, password))
    except Exception as error:  # Aggregate failure only; no credentials or source values.
        return BaselineOutput(
            name="B3_GDS_LOUVAIN",
            status="FAILED",
            scores={record.ticket_id: 0.0 for record in records},
            detail=f"{type(error).__name__}: GDS Louvain evaluation failed",
        )
