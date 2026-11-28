"""Deterministic display-only candidate families and evidence summaries."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable


REASON_CODE_BY_SIGNAL = {
    "refund_account_hash": "SHARED_REFUND_ACCOUNT",
    "phone_hash": "SHARED_PHONE",
    "device_id_hash": "SHARED_DEVICE",
    "address_hash": "SHARED_ADDRESS",
}


def reason_code(signal: str) -> str | None:
    if signal in REASON_CODE_BY_SIGNAL:
        return REASON_CODE_BY_SIGNAL[signal]
    if signal.startswith("ip_window:"):
        return "IP_SAME_WINDOW"
    return None


def build_evidence_reasons(
    account_ids: Iterable[str],
    candidate_performances: Iterable[str],
    shared_groups: Iterable[tuple[str, str, set[str]]],
    member_performances: dict[str, set[str]] | None = None,
    high_quantity_performances: dict[str, set[str]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Summarize qualifying shared-token groups without retaining token values.

    Candidate ``count`` is the number of distinct members seen in at least one
    qualifying group for the reason. A member's ``peerCount`` is the largest
    number of other members in one qualifying token group for that reason.
    Strength rank orders by count, then performance coverage, then code.

    HIGH_QUANTITY is emitted only when the caller supplies verified active
    purchase observations for at least two distinct performances. This display
    reason does not depend on Track 2 resale-seat overlap and never changes score.
    """
    members = sorted(set(account_ids))
    metrics: dict[str, dict] = {}
    member_metrics: dict[str, dict[str, dict]] = {account: {} for account in members}
    observed_performances: dict[str, set[str]] = {account: set() for account in members}

    for signal, performance_id, owners in shared_groups:
        code = reason_code(signal)
        if code is None or len(owners) < 2:
            continue
        metric = metrics.setdefault(code, {"accounts": set(), "performances": set()})
        metric["accounts"].update(owners)
        metric["performances"].add(performance_id)
        for account in owners:
            observed_performances[account].add(performance_id)
            item = member_metrics[account].setdefault(code, {"peer_count": 0, "performances": set()})
            item["peer_count"] = max(item["peer_count"], len(owners) - 1)
            item["performances"].add(performance_id)

    repeated = set(candidate_performances)
    repeated_members: dict[str, set[str]] = {}
    if len(repeated) >= 2:
        for account in members:
            performances = (
                observed_performances[account]
                | (member_performances or {}).get(account, set())
            ) & repeated
            if len(performances) >= 2:
                repeated_members[account] = performances
                member_metrics[account]["REPEATED_PERFORMANCES"] = {
                    "peer_count": 0,
                    "performances": performances,
                }
    if repeated_members:
        metrics["REPEATED_PERFORMANCES"] = {
            "accounts": set(repeated_members),
            "performances": set().union(*repeated_members.values()),
        }

    high_quantity_members: dict[str, set[str]] = {}
    for account in members:
        performances = set((high_quantity_performances or {}).get(account, set()))
        if len(performances) >= 2:
            high_quantity_members[account] = performances
            member_metrics[account]["HIGH_QUANTITY"] = {
                "peer_count": 0,
                "performances": performances,
            }
    if high_quantity_members:
        metrics["HIGH_QUANTITY"] = {
            "accounts": set(high_quantity_members),
            "performances": set().union(*high_quantity_members.values()),
        }

    ordered = sorted(metrics.items(), key=lambda item: (
        -len(item[1]["accounts"]), -len(item[1]["performances"]), item[0],
    ))
    candidate_reasons = [
        {"code": code, "count": len(value["accounts"]),
         "performances": sorted(value["performances"]), "strengthRank": rank}
        for rank, (code, value) in enumerate(ordered, start=1)
    ]
    member_reasons = []
    for account in members:
        reasons = [
            {"code": code, "peerCount": value["peer_count"],
             "performances": sorted(value["performances"])}
            for code, value in sorted(member_metrics[account].items())
        ]
        member_reasons.append({"accountReferenceHmac": account, "reasons": reasons})
    return candidate_reasons, member_reasons


def assign_candidate_families(
    account_sets: dict[str, set[str]], scores: dict[str, float],
) -> dict[str, dict]:
    """Return immediate-parent and root-family metadata for strict subsets."""
    ids = sorted(account_sets)
    ordered_by_size = sorted(ids, key=lambda candidate_id: (len(account_sets[candidate_id]), candidate_id))
    candidates_by_account: dict[str, list[str]] = defaultdict(list)
    for candidate_id in ordered_by_size:
        for account in account_sets[candidate_id]:
            candidates_by_account[account].append(candidate_id)
    parents: dict[str, str | None] = {}
    for candidate_id in ids:
        accounts = account_sets[candidate_id]
        parent = None
        rarest_account = min(accounts, key=lambda account: len(candidates_by_account[account]))
        for possible in candidates_by_account[rarest_account]:
            possible_accounts = account_sets[possible]
            if len(possible_accounts) <= len(accounts):
                continue
            if accounts < possible_accounts:
                parent = possible
                break
        parents[candidate_id] = parent

    child_counts = {candidate_id: 0 for candidate_id in ids}
    for parent in parents.values():
        if parent is not None:
            child_counts[parent] += 1

    roots: dict[str, str] = {}
    for candidate_id in ids:
        path = []
        current = candidate_id
        while parents[current] is not None and current not in roots:
            path.append(current)
            current = parents[current]
        resolved_root = roots.get(current, current)
        roots[candidate_id] = resolved_root
        for item in path:
            roots[item] = resolved_root
    family_scores: dict[str, float] = defaultdict(float)
    for candidate_id, family_id in roots.items():
        family_scores[family_id] = max(family_scores[family_id], scores[candidate_id])
    return {
        candidate_id: {
            "parent_candidate_id": parents[candidate_id],
            "family_id": roots[candidate_id],
            "child_count": child_counts[candidate_id],
            "family_score": family_scores[roots[candidate_id]],
        }
        for candidate_id in ids
    }
