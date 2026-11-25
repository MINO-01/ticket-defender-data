"""Offline exp1/exp2 comparison with a clearly labeled component proxy.

Analysis candidates, A6 recurrence groups, and ablations are frozen before truth
is read. This path never claims that its connected-component proxy is NetworkX
or GDS Louvain.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from services.track1_scoring import build_account_token_graph, community_shared_tokens, score_v2, signal_coverage
from services.track1_scoring_exp2 import (
    STRONG_RECURRENCE_SIGNALS,
    RecurrenceCluster,
    build_account_token_graph_exp2,
    quantity_context,
    recurrence_components,
    score_exp2,
)


@dataclass(frozen=True)
class OfflineTicket:
    account_id: str
    reservation_no: str
    scope: str
    purchased_at: datetime | None
    payment_hash: str | None = None
    address_hash: str | None = None
    device_id_hash: str | None = None
    ip_hash: str | None = None
    refund_account_hash: str | None = None
    phone_hash: str | None = None
    ip_network_type: str | None = None

    @property
    def analysis_scope(self) -> str:
        return self.scope


def _read_tickets(path: Path) -> list[OfflineTicket]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("ticket input must be a non-empty JSON array")
    rows = []
    for value in payload:
        if not isinstance(value, dict):
            raise ValueError("ticket row must be an object")
        purchased = value.get("purchasedAt")
        parsed = datetime.fromisoformat(purchased.replace("Z", "+00:00")) if isinstance(purchased, str) else None
        if parsed is not None and (parsed.tzinfo is None or parsed.utcoffset() is None):
            raise ValueError("purchasedAt must include a UTC offset")
        rows.append(OfflineTicket(
            account_id=_text(value, "accountId"), reservation_no=_text(value, "reservationNo"),
            scope=_text(value, "performanceId"), purchased_at=parsed,
            payment_hash=_optional(value, "paymentHash"), address_hash=_optional(value, "addressHash"),
            device_id_hash=_optional(value, "deviceIdHash"), ip_hash=_optional(value, "ipHash"),
            refund_account_hash=_optional(value, "refundAccountHash"), phone_hash=_optional(value, "phoneHash"),
            ip_network_type=_optional(value, "ipNetworkType"),
        ))
    identities = [(row.scope, row.account_id, row.reservation_no) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate offline ticket identity")
    return rows


def _text(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} is required")
    return item


def _optional(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is not None and not isinstance(item, str):
        raise ValueError(f"{key} must be a string when present")
    return item


def _components(account_count: int, edges: Iterable[dict[str, object]]) -> list[list[int]]:
    """Deterministic connected-component proxy for pure formula smoke tests."""
    parent = list(range(account_count))
    by_token: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if type(source) is not int or type(target) is not int:
            raise ValueError("invalid graph edge")
        by_token.setdefault(target, []).append(source)

    def root(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    for owners in by_token.values():
        for value in owners[1:]:
            left, right = root(owners[0]), root(value)
            if left != right:
                parent[right] = left
    grouped: dict[int, list[int]] = {}
    for index in range(account_count):
        grouped.setdefault(root(index), []).append(index)
    return [members for members in grouped.values() if len(members) >= 2]


def _exp1(tickets: list[OfflineTicket]) -> list[dict[str, object]]:
    graph = build_account_token_graph(tickets)
    clusters = []
    for members in _components(len(graph.accounts), graph.edges):
        accounts = [graph.accounts[index] for index in members]
        shared = community_shared_tokens(accounts, graph)
        if any(shared.values()):
            clusters.append({
                "accounts": sorted(account.key[1] for account in accounts),
                "performances": sorted({account.key[0] for account in accounts}),
                "score": score_v2(len(accounts), signal_coverage(accounts, shared)),
            })
    return sorted(clusters, key=lambda row: (-float(row["score"]), tuple(row["accounts"])))


def _exp2(tickets: list[OfflineTicket]) -> list[dict[str, object]]:
    """Apply A6's research union-find after per-performance exp2 projections."""
    graph = build_account_token_graph_exp2(tickets)
    token_owners: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    for edge in graph.edges:
        account_index = int(edge["account_index"])
        token_index = int(edge["target"]) - len(graph.accounts)
        token_owners[graph.tokens[token_index]].add(account_index)

    recurrence_clusters = []
    detail_by_cluster: dict[str, dict[str, Any]] = {}
    for members in _components(len(graph.accounts), graph.edges):
        member_accounts = [graph.accounts[index] for index in members]
        scopes = {account.scope for account in member_accounts}
        if len(scopes) != 1:
            raise ValueError("exp2 projection crossed a performance scope")
        scope = next(iter(scopes))
        member_set = set(members)
        account_ids = frozenset(account.account_id for account in member_accounts)
        covered: dict[str, set[str]] = defaultdict(set)
        damping_values: dict[str, list[float]] = defaultdict(list)
        recurrence_tokens: dict[str, set[str]] = defaultdict(set)
        for token in graph.tokens:
            if token[1] not in STRONG_RECURRENCE_SIGNALS:
                continue
            owners = token_owners[token] & member_set
            if len(owners) < 2:
                continue
            covered[token[1]].update(graph.accounts[index].account_id for index in owners)
            damping_values[token[1]].append(graph.token_damping[token])
            recurrence_tokens[token[1]].add(token[2])
        cluster_id = hashlib.sha256((scope + "\n" + "\n".join(sorted(account_ids))).encode()).hexdigest()
        recurrence_clusters.append(RecurrenceCluster(
            cluster_id=cluster_id, scope=scope, account_ids=account_ids,
            tokens={signal: frozenset(values) for signal, values in recurrence_tokens.items()},
        ))
        detail_by_cluster[cluster_id] = {"accounts": account_ids, "covered": covered, "damping": damping_values}

    output = []
    for component in recurrence_components(recurrence_clusters):
        details = [detail_by_cluster[cluster_id] for cluster_id in component.cluster_ids]
        accounts = component.account_ids
        coverage: dict[str, float] = {}
        mean_damping: dict[str, float] = {}
        for signal in sorted(set().union(*(detail["covered"].keys() for detail in details))):
            covered_accounts = set().union(*(detail["covered"].get(signal, set()) for detail in details))
            values = [value for detail in details for value in detail["damping"].get(signal, [])]
            coverage[signal] = min(len(covered_accounts) / len(accounts), 1.0)
            mean_damping[signal] = sum(values) / len(values)
        score = score_exp2(
            len(accounts), coverage, mean_damping, quantity_context(None), len(component.scopes),
        )
        output.append({
            "accounts": sorted(accounts), "performances": sorted(component.scopes),
            "sourceClusters": list(component.cluster_ids), "score": score.score,
            "availableSignals": list(score.available_signals), "L": score.link, "G": score.size,
            "B": score.behavior, "Q": score.quantity, "R": score.recurrence,
        })
    return sorted(output, key=lambda row: (-float(row["score"]), tuple(row["accounts"])))


def _truth_roles(truth: dict[str, str]) -> tuple[dict[str, set[str]], set[str], set[str], int]:
    groups: dict[str, set[str]] = defaultdict(set)
    normal: set[str] = set()
    ambiguous: set[str] = set()
    for account, role in truth.items():
        if role.startswith("SCALPER_"):
            groups[role].add(account)
        elif role.startswith("NORMAL_"):
            normal.add(account)
        else:
            ambiguous.add(account)
    return dict(groups), normal, ambiguous, len(ambiguous)


def _matching(predicted: list[set[str]], truth_groups: dict[str, set[str]]) -> int:
    """Maximum-cardinality group matching; deterministic IoU and ID tie order."""
    truth_ids = sorted(truth_groups)
    truth_index = {group_id: index for index, group_id in enumerate(truth_ids)}
    account_groups: dict[str, list[str]] = defaultdict(list)
    for group_id, accounts in truth_groups.items():
        for account in accounts:
            account_groups[account].append(group_id)
    neighbors: list[list[int]] = []
    for predicted_group in predicted:
        intersections: dict[str, int] = defaultdict(int)
        for account in predicted_group:
            for group_id in account_groups.get(account, []):
                intersections[group_id] += 1
        choices = []
        for group_id, intersection in intersections.items():
            true_count = len(truth_groups[group_id])
            union = len(predicted_group) + true_count - intersection
            iou = intersection / union if union else 0.0
            sensitivity = intersection / true_count if true_count else 0.0
            if iou >= 0.5 and sensitivity >= 0.8:
                choices.append((group_id, iou))
        choices.sort(key=lambda item: (-item[1], item[0]))
        neighbors.append([truth_index[group_id] for group_id, _ in choices])
    matched_truth: dict[int, int] = {}

    def augment(pred_index: int, seen: set[int]) -> bool:
        for truth_id in neighbors[pred_index]:
            if truth_id in seen:
                continue
            seen.add(truth_id)
            if truth_id not in matched_truth or augment(matched_truth[truth_id], seen):
                matched_truth[truth_id] = pred_index
                return True
        return False

    for pred_index in range(len(predicted)):
        augment(pred_index, set())
    return len(matched_truth)


def _metrics(clusters: list[dict[str, object]], truth: dict[str, str]) -> dict[str, object]:
    scalper_groups, normal_accounts, ambiguous_accounts, ambiguous_count = _truth_roles(truth)
    scalper_accounts = set().union(*scalper_groups.values()) if scalper_groups else set()
    known_accounts = scalper_accounts | normal_accounts
    output: dict[str, object] = {"ambiguousTruthAccountCountExcluded": ambiguous_count}
    for limit in (5, 10, 20):
        selected = clusters[:limit]
        known_clusters = [set(cluster["accounts"]) & known_accounts for cluster in selected]
        known_clusters = [members for members in known_clusters if members]
        positive_clusters = sum(1 for members in known_clusters if len(members & scalper_accounts) * 2 > len(members))
        cluster_precision = positive_clusters / len(known_clusters) if known_clusters else None
        selected_accounts = set().union(*(set(cluster["accounts"]) for cluster in selected)) if selected else set()
        selected_known = selected_accounts & known_accounts
        account_precision = len(selected_known & scalper_accounts) / len(selected_known) if selected_known else None
        normal_false_selection = len(selected_accounts & normal_accounts) / len(normal_accounts) if normal_accounts else None
        predicted_for_matching = [members for members in known_clusters]
        matched = _matching(predicted_for_matching, scalper_groups)
        output[f"clusterPrecisionAt{limit}"] = cluster_precision
        output[f"accountPrecisionWithinTop{limit}Clusters"] = account_precision
        output[f"normalAccountFalseSelectionRateAt{limit}Clusters"] = normal_false_selection
        output[f"groupRecallAt{limit}"] = matched / len(scalper_groups) if scalper_groups else None
        output[f"groupPrecisionAt{limit}"] = matched / len(predicted_for_matching) if predicted_for_matching else None
        # Retain the original field name as the explicitly cluster-unit precision.
        output[f"precisionAt{limit}"] = cluster_precision

    all_predicted = [set(cluster["accounts"]) & known_accounts for cluster in clusters]
    predicted_pairs = sum(math.comb(len(members), 2) for members in all_predicted)
    true_pairs = sum(math.comb(len(accounts), 2) for accounts in scalper_groups.values())
    true_positive_pairs = 0
    for members in all_predicted:
        for accounts in scalper_groups.values():
            true_positive_pairs += math.comb(len(members & accounts), 2)
    pair_precision = true_positive_pairs / predicted_pairs if predicted_pairs else None
    pair_recall = true_positive_pairs / true_pairs if true_pairs else None
    output["pairwisePrecision"] = pair_precision
    output["pairwiseRecall"] = pair_recall
    output["pairwiseF1"] = (
        2 * pair_precision * pair_recall / (pair_precision + pair_recall)
        if pair_precision is not None and pair_recall is not None and pair_precision + pair_recall > 0 else None
    )
    normal_ranks = [index for index, cluster in enumerate(clusters, start=1) if set(cluster["accounts"]) & normal_accounts]
    output["normalCounterexampleBestRank"] = min(normal_ranks) if normal_ranks else None
    output["predictedClusterCount"] = len(clusters)
    output["predictedAccountCount"] = len(set().union(*(set(cluster["accounts"]) for cluster in clusters))) if clusters else 0
    output["truthScalperGroupCount"] = len(scalper_groups)
    output["truthNormalAccountCount"] = len(normal_accounts)
    return output


def compare_exp2(ticket_path: Path, truth_path: Path, output_dir: Path) -> Path:
    """Freeze all analysis outputs before reading the separate truth file."""
    if ticket_path.resolve() == truth_path.resolve():
        raise ValueError("ticket input and truth file must be separate")
    ticket_bytes = ticket_path.read_bytes()
    tickets = _read_tickets(ticket_path)
    baseline = _exp1(tickets)
    exp2 = _exp2(tickets)
    ablations = {
        signal: _exp2([replace(ticket, **{signal: None}) for ticket in tickets])
        for signal in ("refund_account_hash", "phone_hash", "device_id_hash", "address_hash", "payment_hash", "ip_hash")
    }
    frozen = {"v2": baseline, "exp2_a6_research": exp2, "ablations": ablations}
    frozen_bytes = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    truth_bytes = truth_path.read_bytes()
    truth = json.loads(truth_bytes)
    if not isinstance(truth, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in truth.items()):
        raise ValueError("truth must be an account-to-role JSON object")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "compare-exp2.json"
    result = {
        "status": "COMPLETED_OFFLINE_COMPONENT_PROXY",
        "algorithmStatus": {"componentProxy": "COMPLETED", "networkxLouvain": "NOT_RUN", "gdsLouvain": "NOT_RUN"},
        "truthReadAfterAnalysis": True,
        "datasetSha256": hashlib.sha256(ticket_bytes).hexdigest(),
        "frozenAnalysisSha256": hashlib.sha256(frozen_bytes).hexdigest(),
        "truthSha256": hashlib.sha256(truth_bytes).hexdigest(),
        "v2": {"scope": "PERFORMANCE_COMPONENT_PROXY", "clusters": baseline, "metrics": _metrics(baseline, truth)},
        "exp2": {"scope": "A6_RECURRENCE_UNION_FIND_RESEARCH_ONLY", "clusters": exp2, "metrics": _metrics(exp2, truth)},
        "ablation": {
            signal: {"clusters": rows, "metrics": _metrics(rows, truth)}
            for signal, rows in ablations.items()
        },
        "metricUnits": {
            "clusterPrecisionAtK": "predicted cluster",
            "accountPrecisionWithinTopKClusters": "distinct account",
            "groupRecallAtK": "truth SCALPER group, one-to-one IoU >= 0.5 and sensitivity >= 0.8",
            "pairwiseF1": "account pairs",
            "normalAccountFalseSelectionRateAtKClusters": "distinct NORMAL account",
        },
        "limitations": [
            "Connected components are not NetworkX Louvain or Neo4j GDS Louvain.",
            "A6 union-find is an offline recurrence research baseline and may merge overlapping groups transitively.",
            "Q is neutral because this comparator does not include a confirmed purchase-limit policy.",
        ],
    }
    target.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return target
