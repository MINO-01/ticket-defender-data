"""CLI orchestration for offline synthetic and IEEE-CIS Track 1 evaluation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from time import perf_counter
from typing import Iterable

from .baselines import b0_random, b1_shared_signal_rule, b2_wcc
from .gds import b3_existing_gds_louvain, b3_legacy_v1_gds_louvain
from .ieee import default_ieee_data_dir, holdout_test_dataset, load_ieee_cis
from .metrics import evaluate
from .models import BaselineOutput, EvaluationDataset
from .results import default_results_dir, write_results
from .synthetic import DEFAULT_SEED, build_synthetic_suite


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_outputs(
    dataset: EvaluationDataset, *, seed: int, gds_uri: str | None, gds_password: str | None
) -> Iterable[tuple[BaselineOutput, int, int | None]]:
    # B0 repetitions use deterministic adjacent seeds and remain separate rows.
    for repetition in range(3):
        started = perf_counter()
        yield b0_random(dataset.records, seed + repetition), round((perf_counter() - started) * 1000), repetition
    for factory in (b1_shared_signal_rule, b2_wcc):
        started = perf_counter()
        yield factory(dataset.records), round((perf_counter() - started) * 1000), None
    started = perf_counter()
    yield b3_existing_gds_louvain(dataset.records, uri=gds_uri, password=gds_password), round((perf_counter() - started) * 1000), None
    # 계정 단위 전환 뒤에도 같은 입력의 티켓 단위 기준선을 별도 행으로 남깁니다.
    started = perf_counter()
    yield b3_legacy_v1_gds_louvain(dataset.records, uri=gds_uri, password=gds_password), round((perf_counter() - started) * 1000), None


def evaluate_datasets(
    datasets: Iterable[EvaluationDataset], *, seed: int, gds_uri: str | None, gds_password: str | None
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for dataset in datasets:
        for output, elapsed_ms, repetition in _run_outputs(
            dataset, seed=seed, gds_uri=gds_uri, gds_password=gds_password
        ):
            row: dict[str, object] = {
                "dataset": dataset.name,
                "baseline": output.name,
                "status": output.status,
                "runtime_ms": elapsed_ms,
                "failure_count": 1 if output.status == "FAILED" else 0,
                "algorithm_version": output.algorithm_version,
                "detail": output.detail,
                "random_seed": seed + repetition if repetition is not None else None,
                "random_repetition": repetition,
            }
            row.update(evaluate(dataset, output))
            rows.append(row)
    return rows


def run_synthetic(args: argparse.Namespace) -> dict[str, Path]:
    password = os.getenv(args.gds_password_env) if args.gds_uri else None
    datasets = build_synthetic_suite(args.seed)
    rows = evaluate_datasets(datasets, seed=args.seed, gds_uri=args.gds_uri, gds_password=password)
    status = "COMPLETED_WITH_GDS_SKIP" if any(row["status"] == "SKIPPED_GDS_ISOLATION" for row in rows) else "COMPLETED"
    return write_results(
        repository=_repository_root(), output_dir=args.output_dir, run_name="track1-synthetic",
        status=status, rows=rows,
        metadata={"suite": "S1-S8", "seed": args.seed, "gds_uri_provided": bool(args.gds_uri)},
    )


def run_ieee(args: argparse.Namespace) -> dict[str, Path]:
    password = os.getenv(args.gds_password_env) if args.gds_uri else None
    max_rows = None if args.full else args.max_rows
    try:
        dataset, split = load_ieee_cis(args.data_dir, max_rows=max_rows)
    except FileNotFoundError:
        return write_results(
            repository=_repository_root(), output_dir=args.output_dir, run_name="track1-ieee-cis",
            status="PENDING_DATASET", rows=[],
            metadata={
                "required_files": ["train_transaction.csv", "train_identity.csv"],
                "data_dir": str(args.data_dir), "source_rows_not_read": True,
            },
        )
    except (OSError, ValueError) as error:
        return write_results(
            repository=_repository_root(), output_dir=args.output_dir, run_name="track1-ieee-cis",
            status="FAILED_INPUT_VALIDATION", rows=[],
            metadata={"error_type": type(error).__name__, "data_dir": str(args.data_dir)},
        )
    heldout = holdout_test_dataset(dataset, split)
    rows = evaluate_datasets((heldout,), seed=args.seed, gds_uri=args.gds_uri, gds_password=password)
    status = "COMPLETED_WITH_GDS_SKIP" if any(row["status"] == "SKIPPED_GDS_ISOLATION" for row in rows) else "COMPLETED"
    return write_results(
        repository=_repository_root(), output_dir=args.output_dir, run_name="track1-ieee-cis",
        status=status, rows=rows,
        metadata={**dataset.metadata, "evaluated_partition": "test", "full_run_explicit": bool(args.full)},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline Track 1 effectiveness evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def shared_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--output-dir", type=Path, default=default_results_dir(), help="External aggregate result directory")
        command.add_argument("--seed", type=int, default=DEFAULT_SEED)
        command.add_argument("--gds-uri", help="Only an isolated local GDS URI, never an application database")
        command.add_argument("--gds-password-env", default="TD_GDS_TEST_PASSWORD", help="Environment variable holding the isolated GDS password")

    synthetic = subparsers.add_parser("synthetic", help="Run fixed-seed S1-S8 suite")
    shared_options(synthetic)
    synthetic.set_defaults(handler=run_synthetic)

    ieee = subparsers.add_parser("ieee", help="Run user-provided local IEEE-CIS CSVs")
    shared_options(ieee)
    ieee.add_argument("--data-dir", type=Path, default=default_ieee_data_dir(), help="External directory containing the two CSV files")
    ieee.add_argument("--max-rows", type=int, default=5000, help="Explicit small transaction sample; default: 5000")
    ieee.add_argument("--full", action="store_true", help="Explicitly permit a full source read; never the default")
    ieee.set_defaults(handler=run_ieee)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = args.handler(args)
    print("status results written:")
    for kind, path in paths.items():
        print(f"{kind}={path}")
    return 0
