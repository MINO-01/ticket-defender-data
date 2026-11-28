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
_EXISTING_VALIDATION_PROJECT = "ticket-defender-validation"
_EXISTING_VALIDATION_CONTAINER = "ticket-defender-validation-neo4j"
_EXISTING_VALIDATION_NETWORK = "ticket-defender-validation_default"
_EXISTING_VALIDATION_VOLUMES = {
    "/data": "ticket-defender-validation_neo4j-data",
    "/logs": "ticket-defender-validation_neo4j-logs",
}
# 공식 배포물 이름입니다. 진위는 이름이 아니라 실행 스크립트의 SHA-256 확인으로 판단합니다.
_EXPECTED_GDS_JAR = "neo4j-graph-data-science-2.6.6.jar"
_EXPECTED_NEO4J_IMAGE = "neo4j:5.19.0"
B3_LEGACY_V1_NAME = "B3_GDS_LOUVAIN_LEGACY_SCORE_V1"


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


def _is_dedicated_existing_validation_container(container: dict, container_id: str) -> bool:
    """기존 검증 인스턴스의 고정된 네트워크와 단독 볼륨을 확인합니다."""

    if (
        container.get("Name") != f"/{_EXISTING_VALIDATION_CONTAINER}"
        or container.get("State", {}).get("Status") != "running"
        or set(container.get("NetworkSettings", {}).get("Networks", {})) != {_EXISTING_VALIDATION_NETWORK}
    ):
        return False

    mounts = container.get("Mounts", [])
    if len(mounts) != 3:
        return False
    mounted_volumes = {}
    destinations = set()
    for mount in mounts:
        destination = mount.get("Destination")
        destinations.add(destination)
        if mount.get("Type") == "volume" and destination in _EXISTING_VALIDATION_VOLUMES:
            if mount.get("Name") != _EXISTING_VALIDATION_VOLUMES[destination] or mount.get("RW") is not True:
                return False
            mounted_volumes[destination] = mount["Name"]
        elif (
            mount.get("Type") != "bind"
            or destination != "/plugins/graph-data-science.jar"
            or mount.get("RW") is not False
            or Path(mount.get("Source", "")).name != _EXPECTED_GDS_JAR
        ):
            return False
    if set(mounted_volumes) != set(_EXISTING_VALIDATION_VOLUMES) or destinations != {
        "/data", "/logs", "/plugins/graph-data-science.jar"
    }:
        return False

    for volume in mounted_volumes.values():
        users = _docker_output([
            "container", "ls", "--all", "--quiet", "--filter", f"volume={volume}",
        ])
        if not users or {item.strip() for item in users.splitlines() if item.strip()} != {container_id}:
            return False
    return True


def verify_isolated_gds_target(uri: str) -> str | None:
    """실행 전 URI가 격리된 테스트 Compose 컨테이너를 가리키는지 읽기 전용으로 확인합니다."""

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
    ids = container_ids.split()
    if len(ids) != len(containers):
        return "NO_VERIFIED_GDS_CONTAINER"
    for container_id, container in zip(ids, containers):
        labels = container.get("Config", {}).get("Labels", {}) or {}
        project = labels.get("com.docker.compose.project", "")
        project_is_isolated = bool(_ISOLATED_PROJECT_PATTERN.fullmatch(project))
        project_is_existing_validation = (
            project == _EXISTING_VALIDATION_PROJECT
            and _is_dedicated_existing_validation_container(container, container_id)
        )
        if (
            labels.get("com.docker.compose.service") != "neo4j"
            or not (project_is_isolated or project_is_existing_validation)
            or container.get("Config", {}).get("Image") != _EXPECTED_NEO4J_IMAGE
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


def _request_from_records(eligible: list[EvaluationRecord]):
    from schemas.dto import Track1AnalysisRequest

    return Track1AnalysisRequest.model_validate({
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


async def _run_legacy_v1_louvain(
    records: tuple[EvaluationRecord, ...], uri: str, password: str
) -> BaselineOutput:
    """평가 전용 score-v1 경로입니다. 같은 입력과 같은 GDS로 비교합니다."""

    from evaluation.track1.legacy_score_v1 import analyze_louvain_v1
    from services.graph_service import GraphService

    scores = {record.ticket_id: 0.0 for record in records}
    labels = {record.ticket_id: f"singleton-{record.ticket_id}" for record in records}
    eligible = [record for record in records if record.has_signal]
    if not eligible:
        return BaselineOutput(B3_LEGACY_V1_NAME, "COMPLETED", scores, labels, "No records had observable signals.")
    service = GraphService(uri, "neo4j", password)
    try:
        response = await analyze_louvain_v1(service.driver, _request_from_records(eligible))
    finally:
        await service.close()
    by_key = {record.key: record.ticket_id for record in records}
    for cluster in response.clusters:
        for member in cluster.members:
            ticket_id = by_key[(member.account_id, member.reservation_no, member.event_id)]
            scores[ticket_id] = max(scores[ticket_id], cluster.risk_score)
            labels[ticket_id] = cluster.cluster_id
    return BaselineOutput(B3_LEGACY_V1_NAME, "COMPLETED", scores, labels, algorithm_version=response.algorithm_version)


def b3_legacy_v1_gds_louvain(
    records: tuple[EvaluationRecord, ...], *, uri: str | None, password: str | None
) -> BaselineOutput:
    """B3와 같은 격리 조건에서 score-v1 기준선을 실행합니다."""

    zero = {record.ticket_id: 0.0 for record in records}
    if not uri or not password:
        return BaselineOutput(B3_LEGACY_V1_NAME, "SKIPPED_GDS_ISOLATION", zero, detail="Set an isolated GDS URI and password to run B3.")
    isolation_failure = verify_isolated_gds_target(uri)
    if isolation_failure is not None:
        return BaselineOutput(B3_LEGACY_V1_NAME, "SKIPPED_GDS_ISOLATION", zero, detail=f"GDS isolation verification failed: {isolation_failure}")
    try:
        return asyncio.run(_run_legacy_v1_louvain(records, uri, password))
    except Exception as error:
        return BaselineOutput(B3_LEGACY_V1_NAME, "FAILED", zero, detail=f"{type(error).__name__}: GDS Louvain evaluation failed")


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
