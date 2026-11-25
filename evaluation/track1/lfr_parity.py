"""Seeded V1A LFR comparison for NetworkX and a verified isolated GDS instance.

Ground-truth communities are held outside both algorithm inputs. The GDS path is
read/write only against the ephemeral local container verified by gds.py.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Sequence

LFR_MU_VALUES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
LFR_SEEDS = tuple(range(20261101, 20261111))
LFR_NODE_COUNTS = (1000, 5000)


def _choose_two(value: int) -> int:
    return value * (value - 1) // 2


def adjusted_rand_index(truth: Sequence[str], predicted: Sequence[str]) -> float:
    if len(truth) != len(predicted) or not truth:
        raise ValueError("partition labels must have equal, non-zero lengths")
    contingency = Counter(zip(truth, predicted, strict=True))
    truth_counts = Counter(truth)
    predicted_counts = Counter(predicted)
    index = sum(_choose_two(value) for value in contingency.values())
    truth_pairs = sum(_choose_two(value) for value in truth_counts.values())
    predicted_pairs = sum(_choose_two(value) for value in predicted_counts.values())
    total_pairs = _choose_two(len(truth))
    if total_pairs == 0:
        return 1.0
    expected = truth_pairs * predicted_pairs / total_pairs
    maximum = (truth_pairs + predicted_pairs) / 2
    denominator = maximum - expected
    return 1.0 if denominator == 0 else (index - expected) / denominator


def normalized_mutual_information(truth: Sequence[str], predicted: Sequence[str]) -> float:
    if len(truth) != len(predicted) or not truth:
        raise ValueError("partition labels must have equal, non-zero lengths")
    total = len(truth)
    truth_counts = Counter(truth)
    predicted_counts = Counter(predicted)
    joint = Counter(zip(truth, predicted, strict=True))
    truth_entropy = -sum((count / total) * math.log(count / total) for count in truth_counts.values())
    predicted_entropy = -sum((count / total) * math.log(count / total) for count in predicted_counts.values())
    if truth_entropy == 0 and predicted_entropy == 0:
        return 1.0
    if truth_entropy == 0 or predicted_entropy == 0:
        return 0.0
    mutual_information = sum(
        (count / total) * math.log((count * total) / (truth_counts[left] * predicted_counts[right]))
        for (left, right), count in joint.items()
    )
    return 2 * mutual_information / (truth_entropy + predicted_entropy)


def _communities(labels: dict[int, str]) -> list[list[int]]:
    grouped: dict[str, list[int]] = {}
    for node, label in sorted(labels.items()):
        grouped.setdefault(label, []).append(node)
    return sorted(grouped.values(), key=lambda members: tuple(members))


def run_lfr(seed: int, mu: float, node_count: int) -> tuple[object, list[str], dict[str, object], float]:
    if seed not in LFR_SEEDS or mu not in LFR_MU_VALUES or node_count not in LFR_NODE_COUNTS:
        raise ValueError("V1A seed, mu, and node count must match the fixed validation plan")
    try:
        import networkx as nx
    except ImportError as exc:
        raise RuntimeError("Install evaluation/track1/requirements-lfr.txt to run V1A") from exc

    started = time.perf_counter()
    generated = nx.LFR_benchmark_graph(
        n=node_count, tau1=3.0, tau2=1.5, mu=mu, average_degree=10,
        min_community=20, max_community=100, seed=seed,
    )
    generation_seconds = time.perf_counter() - started
    truth_groups = {
        tuple(sorted(generated.nodes[node]["community"]))
        for node in generated.nodes
    }
    truth_key = {members: f"truth-{index}" for index, members in enumerate(sorted(truth_groups))}
    truth_by_node = {
        node: truth_key[tuple(sorted(generated.nodes[node]["community"]))]
        for node in generated.nodes
    }
    # Build a fresh topology-only graph. Truth labels never enter either algorithm.
    analysis = nx.Graph()
    analysis.add_nodes_from(sorted(generated.nodes))
    analysis.add_weighted_edges_from((min(a, b), max(a, b), 1.0) for a, b in generated.edges)
    ordered_nodes = sorted(analysis.nodes)
    truth_labels = [truth_by_node[node] for node in ordered_nodes]
    edge_material = "\n".join(f"{left}:{right}:1" for left, right in sorted(analysis.edges)).encode()
    truth_material = "\n".join(f"{node}:{truth_by_node[node]}" for node in ordered_nodes).encode()
    metadata = {
        "topologySha256": hashlib.sha256(edge_material).hexdigest(),
        "truthSha256": hashlib.sha256(truth_material).hexdigest(),
        "nodeCount": len(ordered_nodes), "edgeCount": analysis.number_of_edges(),
        "seed": seed, "mu": mu, "tau1": 3.0, "tau2": 1.5,
        "averageDegree": 10, "minCommunity": 20, "maxCommunity": 100,
    }
    return analysis, truth_labels, metadata, generation_seconds


def _networkx_louvain(graph: object, seed: int) -> tuple[list[str], float, str, float, int]:
    import networkx as nx

    started = time.perf_counter()
    communities = nx.community.louvain_communities(
        graph, weight="weight", resolution=1.0, threshold=1e-7, seed=seed,
    )
    elapsed = time.perf_counter() - started
    ordered = sorted(communities, key=lambda row: tuple(sorted(row)))
    labels = {node: f"nx-{index}" for index, group in enumerate(ordered) for node in group}
    return ([labels[node] for node in sorted(graph.nodes)], elapsed, nx.__version__,
            nx.community.modularity(graph, ordered, weight="weight", resolution=1.0), len(ordered))


def _gds_louvain(graph: object, seed: int, uri: str, password: str) -> tuple[list[str], float, str]:
    from neo4j import GraphDatabase
    from evaluation.track1.gds import verify_isolated_gds_target

    isolation = verify_isolated_gds_target(uri)
    if isolation:
        raise RuntimeError(f"GDS isolation check failed: {isolation}")
    run_id = "lfr" + os.urandom(12).hex()
    graph_name = "tdlfr" + os.urandom(8).hex()
    driver = GraphDatabase.driver(uri, auth=("neo4j", password), connection_timeout=5)
    nodes_created = False
    projection_created = False
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            version_record = session.run("RETURN gds.version() AS version").single()
            gds_version = str(version_record["version"])
            if gds_version != "2.6.6":
                raise RuntimeError("V1A is pinned to GDS 2.6.6")
            nodes = [{"node": int(node)} for node in sorted(graph.nodes)]
            edges = [{"source": int(a), "target": int(b), "weight": float(data.get("weight", 1.0))}
                     for a, b, data in graph.edges(data=True)]
            session.run(
                "UNWIND $rows AS row CREATE (:TDLfrEvalNode {runId:$runId,nodeIndex:row.node})",
                rows=nodes, runId=run_id,
            ).consume()
            nodes_created = True
            session.run("""
                UNWIND $rows AS row
                MATCH (a:TDLfrEvalNode {runId:$runId,nodeIndex:row.source})
                MATCH (b:TDLfrEvalNode {runId:$runId,nodeIndex:row.target})
                CREATE (a)-[:TDLfrEvalEdge {runId:$runId,weight:row.weight}]->(b)
                """, rows=edges, runId=run_id).consume()
            projection = session.run("""
                MATCH (source:TDLfrEvalNode {runId:$runId})
                OPTIONAL MATCH (source)-[r:TDLfrEvalEdge {runId:$runId}]->(target:TDLfrEvalNode {runId:$runId})
                WITH gds.graph.project(
                  $graphName, source, target,
                  {
                    sourceNodeProperties: source {nodeIndex:source.nodeIndex},
                    targetNodeProperties: target {nodeIndex:target.nodeIndex},
                    relationshipType:'TDLfrEvalEdge',
                    relationshipProperties: r {weight:r.weight}
                  },
                  {undirectedRelationshipTypes:['TDLfrEvalEdge']}
                ) AS graph
                RETURN graph.nodeCount AS nodeCount, graph.relationshipCount AS relationshipCount
                """, graphName=graph_name, runId=run_id).single()
            if not projection or projection["nodeCount"] != graph.number_of_nodes() or projection["relationshipCount"] != 2 * graph.number_of_edges():
                raise RuntimeError("GDS LFR projection was incomplete")
            projection_created = True
            started = time.perf_counter()
            result = session.run("""
                CALL gds.louvain.stream($graphName, {
                  relationshipWeightProperty:'weight',
                  concurrency:1, maxLevels:10, tolerance:1e-7
                }) YIELD nodeId, communityId
                RETURN gds.util.nodeProperty($graphName,nodeId,'nodeIndex') AS nodeIndex, communityId
                """, graphName=graph_name, seed=seed)
            labels = {int(row["nodeIndex"]): f"gds-{row['communityId']}" for row in result}
            elapsed = time.perf_counter() - started
            if sorted(labels) != sorted(graph.nodes):
                raise RuntimeError("GDS LFR Louvain omitted nodes")
            return [labels[node] for node in sorted(graph.nodes)], elapsed, gds_version
    finally:
        try:
            if projection_created or nodes_created:
                with driver.session() as session:
                    if projection_created:
                        session.run("CALL gds.graph.drop($graphName,false) YIELD graphName RETURN graphName", graphName=graph_name).consume()
                    if nodes_created:
                        session.run("MATCH (n:TDLfrEvalNode {runId:$runId}) DETACH DELETE n", runId=run_id).consume()
        finally:
            driver.close()


def compare_lfr(seed: int, mu: float, output_path: Path, uri: str | None,
                password: str | None, node_count: int) -> Path:
    if output_path.exists():
        raise FileExistsError("Refusing to overwrite an existing evaluation artifact")
    result: dict[str, object] = {
        "status": "PENDING",
        "dataset": {"seed": seed, "mu": mu, "nodeCount": node_count},
        "networkx": {"status": "NOT_RUN"}, "gds": {"status": "NOT_RUN"},
        "truthPassedToAlgorithms": False,
        "limitations": ["LFR ARI/NMI measure synthetic graph recovery, not ticket-ledger detector accuracy."],
    }
    try:
        graph, truth, dataset, generation_seconds = run_lfr(seed, mu, node_count)
        result["dataset"] = dataset
        result["generationRuntimeSeconds"] = generation_seconds
        nx_labels, nx_runtime, nx_version, nx_modularity, nx_count = _networkx_louvain(graph, seed)
        result["networkx"] = {
            "status": "COMPLETED", "version": nx_version, "runtimeSeconds": nx_runtime,
            "adjustedRandIndex": adjusted_rand_index(truth, nx_labels),
            "normalizedMutualInformation": normalized_mutual_information(truth, nx_labels),
            "modularity": nx_modularity, "communityCount": nx_count,
        }
        if uri and password:
            gds_labels, gds_runtime, gds_version = _gds_louvain(graph, seed, uri, password)
            import networkx as nx
            gds_groups = _community_sets({node: gds_labels[index] for index, node in enumerate(sorted(graph.nodes))})
            result["gds"] = {
                "status": "COMPLETED", "version": gds_version, "runtimeSeconds": gds_runtime,
                "adjustedRandIndex": adjusted_rand_index(truth, gds_labels),
                "normalizedMutualInformation": normalized_mutual_information(truth, gds_labels),
                "modularity": nx.community.modularity(graph, gds_groups, weight="weight", resolution=1.0),
                "communityCount": len(gds_groups),
            }
            result["algorithmParity"] = {
                "adjustedRandIndex": adjusted_rand_index(nx_labels, gds_labels),
                "normalizedMutualInformation": normalized_mutual_information(nx_labels, gds_labels),
            }
            result["status"] = "COMPLETED"
        else:
            result["status"] = "COMPLETED_NETWORKX_ONLY_BLOCKED_GDS"
            result["gds"] = {"status": "BLOCKED_GDS_NOT_CONFIGURED"}
    except Exception as error:
        result["status"] = "FAILED_LFR_GENERATION" if type(error).__module__.startswith("networkx") else "FAILED_EVALUATION"
        result["failureType"] = type(error).__name__
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a seeded NetworkX/GDS V1A LFR comparison.")
    parser.add_argument("--seed", type=int, default=20261101, choices=LFR_SEEDS)
    parser.add_argument("--mu", type=float, default=0.1, choices=LFR_MU_VALUES)
    parser.add_argument("--nodes", type=int, default=1000, choices=LFR_NODE_COUNTS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--matrix", action="store_true", help="Run the complete preregistered 2 x 6 x 10 V1A matrix")
    parser.add_argument("--gds-uri", help="Verified loopback URI of the isolated GDS container")
    parser.add_argument("--gds-password-env", default="TD_GDS_TEST_PASSWORD")
    args = parser.parse_args()
    password = os.environ.get(args.gds_password_env) if args.gds_uri else None
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite an existing evaluation artifact")
    cases = ((size, mu, seed) for size in LFR_NODE_COUNTS for mu in LFR_MU_VALUES for seed in LFR_SEEDS) if args.matrix else ((args.nodes, args.mu, args.seed),)
    if args.matrix:
        for node_count, mu, seed in cases:
            trial_path = args.output.with_name(f"{args.output.stem}-n{node_count}-mu{mu:.1f}-seed{seed}{args.output.suffix}")
            if trial_path.exists():
                raise FileExistsError("Refusing to overwrite an existing evaluation artifact")
        cases = ((size, mu, seed) for size in LFR_NODE_COUNTS for mu in LFR_MU_VALUES for seed in LFR_SEEDS)
    rows = []
    for node_count, mu, seed in cases:
        trial_path = args.output if not args.matrix else args.output.with_name(
            f"{args.output.stem}-n{node_count}-mu{mu:.1f}-seed{seed}{args.output.suffix}"
        )
        if trial_path.exists():
            raise FileExistsError("Refusing to overwrite an existing evaluation artifact")
        compare_lfr(seed, mu, trial_path, args.gds_uri, password, node_count)
        rows.append(json.loads(trial_path.read_text(encoding="utf-8")))
    if args.matrix:
        if args.output.exists():
            raise FileExistsError("Refusing to overwrite an existing V1A matrix artifact")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        complete = all(row["status"] == "COMPLETED" for row in rows)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "status": "COMPLETED_MATRIX" if complete else "MATRIX_HAS_BLOCKED_OR_FAILED_TRIALS",
                "plannedTrialCount": 120,
                "completedTrialCount": sum(row["status"] == "COMPLETED" for row in rows),
                "trials": rows,
            }, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
