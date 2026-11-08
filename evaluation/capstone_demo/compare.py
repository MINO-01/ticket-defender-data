"""같은 원장 입력에 score-v1과 score-v2를 적용해 계정 단위로 비교합니다.

정답은 지표 계산에만 쓰고 분석 함수에는 전달하지 않습니다. 격리 GDS가 없으면
Louvain 행은 SKIPPED_GDS_ISOLATION으로 남깁니다. OFFLINE_WCC_PROXY는 연결요소로
파티션을 대신한 투영과 점수 비교용 값이며 Louvain 실측 성능이 아닙니다.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
from typing import Callable, Mapping, Sequence
import uuid

from schemas.dto import Track1AnalysisRequest, Track1Cluster
from services.track1_scoring import build_account_token_graph, clusters_from_account_communities

from evaluation.track1.gds import verify_isolated_gds_target
from evaluation.track1.legacy_score_v1 import legacy_clusters_from_communities, legacy_edges


PROXY_ENGINE = "OFFLINE_WCC_PROXY_NOT_LOUVAIN"
GDS_ENGINE = "NEO4J_GDS_LOUVAIN"
POSITIVE_PATTERNS = ("R1", "R2", "R3")
NORMAL_SHARED_PATTERNS = ("N1", "N2")
AMBIGUOUS_PATTERN = "N3"
K_VALUES = (10, 17, 30)


def _components(edges: Sequence[Mapping[str, int]]) -> dict[int, int]:
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for edge in edges:
        first, second = find(edge["source"]), find(edge["target"])
        if first != second:
            parent[max(first, second)] = min(first, second)
    return {node: find(node) for node in list(parent)}


def proxy_v1_clusters(request: Track1AnalysisRequest) -> list[Track1Cluster]:
    edges = legacy_edges(request)
    communities = _components(edges)
    records = [
        {"ticketIndex": index, "communityId": communities[index]} for index in range(len(request.tickets))
    ]
    return legacy_clusters_from_communities(request, records)


def proxy_v2_clusters(request: Track1AnalysisRequest) -> list[Track1Cluster]:
    graph = build_account_token_graph(request.tickets)
    communities = _components(graph.edges)
    records = [
        {"accountIndex": index, "communityId": communities[index]} for index in range(len(graph.accounts))
    ]
    return clusters_from_account_communities(request, graph, records)


def load_request(input_dir: Path) -> tuple[Track1AnalysisRequest, dict, str]:
    raw = (input_dir / "track1_ingest.json").read_bytes()
    digest = sha256(raw).hexdigest()
    request = Track1AnalysisRequest.model_validate({
        "requestId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"track1-compare:{digest}")),
        "tickets": json.loads(raw)["tickets"],
    })
    answer = json.loads((input_dir / "answer_key.json").read_text(encoding="utf-8"))
    return request, answer, digest


def _pattern_accounts(answer: Mapping) -> dict[str, str]:
    return {
        member["accountId"]: pattern["patternId"]
        for pattern in answer["patterns"] for member in pattern["members"]
    }


def account_ranking(
    request: Track1AnalysisRequest, clusters: Sequence[Track1Cluster]
) -> tuple[list[str], dict[str, float]]:
    """점수 내림차순, 계정 ID 오름차순으로 전체 계정을 정렬합니다."""

    scores = {ticket.account_id: 0.0 for ticket in request.tickets}
    for cluster in clusters:
        for member in cluster.members:
            scores[member.account_id] = max(scores[member.account_id], cluster.risk_score)
    return sorted(scores, key=lambda account: (-scores[account], account)), scores


def _at_k(
    ranking: Sequence[str], scores: Mapping[str, float], patterns: Mapping[str, str], k: int, positives: int
) -> dict[str, object]:
    selected = [account for account in ranking[:k] if scores[account] > 0]
    hits = sum(1 for account in selected if patterns.get(account) in POSITIVE_PATTERNS)
    normal = sum(1 for account in selected if patterns.get(account) in NORMAL_SHARED_PATTERNS)
    return {
        "k": k,
        "selected": len(selected),
        "precisionAtK": hits / len(selected) if selected else None,
        "recallAtK": hits / positives if positives else None,
        "normalSharedInTop": normal,
        "normalSharedTopRate": normal / len(selected) if selected else None,
    }


def evaluate_clusters(
    request: Track1AnalysisRequest, answer: Mapping, clusters: Sequence[Track1Cluster]
) -> dict[str, object]:
    """N3는 기본 이진 지표에서 제외하되, 음성 처리한 지표와 개별 표를 항상 함께 냅니다."""

    patterns = _pattern_accounts(answer)
    ranking, scores = account_ranking(request, clusters)
    positions = {account: position for position, account in enumerate(ranking, start=1)}
    positives = sum(1 for pattern in patterns.values() if pattern in POSITIVE_PATTERNS)
    without_n3 = [account for account in ranking if patterns.get(account) != AMBIGUOUS_PATTERN]

    by_pattern = defaultdict(list)
    for account, pattern in patterns.items():
        by_pattern[pattern].append(account)
    pattern_table = {}
    for pattern, accounts in sorted(by_pattern.items()):
        detected = [account for account in accounts if scores[account] > 0]
        values = [scores[account] for account in detected]
        pattern_table[pattern] = {
            "accounts": len(accounts),
            "detectedAccounts": len(detected),
            "minScore": min(values, default=None),
            "maxScore": max(values, default=None),
            "bestRank": min((positions[account] for account in detected), default=None),
            "worstRank": max((positions[account] for account in detected), default=None),
        }

    multi_ticket_only = sum(1 for cluster in clusters if len({m.account_id for m in cluster.members}) == 1)
    independent_flagged = sum(1 for account, value in scores.items() if value > 0 and account not in patterns)
    return {
        "clusterCount": len(clusters),
        "singleAccountClusterCount": multi_ticket_only,
        "independentAccountsFlagged": independent_flagged,
        "binaryExcludingN3": [_at_k(without_n3, scores, patterns, k, positives) for k in K_VALUES],
        "binaryN3AsNegative": [_at_k(ranking, scores, patterns, k, positives) for k in K_VALUES],
        "patternTable": pattern_table,
    }


def _run_engine(
    name: str, version: str, request: Track1AnalysisRequest, answer: Mapping,
    builder: Callable[[Track1AnalysisRequest], list[Track1Cluster]],
) -> dict[str, object]:
    started = perf_counter()
    clusters = builder(request)
    elapsed = round((perf_counter() - started) * 1000, 3)
    return {
        "engine": name, "scoreVersion": version, "status": "COMPLETED",
        "runtimeMs": elapsed, **evaluate_clusters(request, answer, clusters),
    }


async def _gds_responses(request: Track1AnalysisRequest, uri: str, password: str):
    from evaluation.track1.legacy_score_v1 import analyze_louvain_v1
    from services.graph_service import GraphService

    service = GraphService(uri, "neo4j", password)
    try:
        legacy = await analyze_louvain_v1(service.driver, request)
        current = await service.analyze_louvain(request)
    finally:
        await service.close()
    return legacy, current


def _gds_rows(request, answer, uri: str | None, password: str | None) -> list[dict[str, object]]:
    reason = "GDS_URI_OR_PASSWORD_NOT_PROVIDED" if not uri or not password else verify_isolated_gds_target(uri)
    if reason is not None:
        return [
            {"engine": GDS_ENGINE, "scoreVersion": version, "status": "SKIPPED_GDS_ISOLATION", "detail": reason}
            for version in ("score-v1", "score-v2")
        ]
    try:
        legacy, current = asyncio.run(_gds_responses(request, uri, password))
    except Exception as error:
        return [
            {"engine": GDS_ENGINE, "scoreVersion": version, "status": "FAILED", "detail": type(error).__name__}
            for version in ("score-v1", "score-v2")
        ]
    return [
        {
            "engine": GDS_ENGINE, "scoreVersion": version, "status": "COMPLETED",
            "algorithmVersion": response.algorithm_version,
            **evaluate_clusters(request, answer, response.clusters),
        }
        for version, response in (("score-v1", legacy), ("score-v2", current))
    ]


def compare_ledger(input_dir: Path, *, gds_uri: str | None = None, gds_password: str | None = None) -> dict[str, object]:
    request, answer, digest = load_request(input_dir)
    return {
        "track1InputSha256": digest,
        "ticketCount": len(request.tickets),
        "accountCount": len({ticket.account_id for ticket in request.tickets}),
        "rows": [
            _run_engine(PROXY_ENGINE, "score-v1", request, answer, proxy_v1_clusters),
            _run_engine(PROXY_ENGINE, "score-v2", request, answer, proxy_v2_clusters),
            *_gds_rows(request, answer, gds_uri, gds_password),
        ],
    }


def write_comparison(results: Mapping[str, object], output_dir: Path, repository: Path) -> Path:
    """집계 결과만 저장소 밖에 기록합니다. 계정 식별자와 토큰은 포함하지 않습니다."""

    resolved = output_dir.resolve()
    if resolved == repository.resolve() or repository.resolve() in resolved.parents:
        raise ValueError("Comparison output must be outside the repository")
    resolved.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = resolved / f"track1-score-comparison-{stamp}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
