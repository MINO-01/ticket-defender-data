"""Safe aggregate result writers. Source rows, proxy values and labels are never written."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Iterable, Mapping


def default_results_dir() -> Path:
    configured = os.getenv("TD_EVALUATION_RESULTS_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ticket-defender" / "evaluation-results"


def _code_sha(repository: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def environment_metadata(repository: Path) -> dict[str, str]:
    return {
        "code_sha": _code_sha(repository),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _assert_outside_repository(output_dir: Path, repository: Path) -> None:
    resolved_output = output_dir.resolve()
    resolved_repository = repository.resolve()
    if resolved_output == resolved_repository or resolved_repository in resolved_output.parents:
        raise ValueError("Evaluation output directory must be outside the Git repository")


def write_results(
    *, repository: Path, output_dir: Path, run_name: str, status: str,
    rows: Iterable[Mapping[str, object]], metadata: Mapping[str, object],
) -> dict[str, Path]:
    """Write aggregate JSON, CSV and a thesis-ready Markdown comparison table."""

    _assert_outside_repository(output_dir, repository)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_rows = list(rows)
    base = f"{run_name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    json_path = output_dir / f"{base}.json"
    csv_path = output_dir / f"{base}.csv"
    markdown_path = output_dir / f"{base}.md"
    payload = {
        "status": status,
        "environment": environment_metadata(repository),
        "metadata": dict(metadata),
        "results": safe_rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    columns = sorted({key for row in safe_rows for key in row})
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(safe_rows)
    display_columns = ["dataset", "baseline", "status", "precision_at_1pct", "recall_at_5pct", "lift_at_1pct", "average_precision", "candidate_coverage", "ari", "nmi"]
    lines = [
        "# Track 1 평가 비교표", "", f"상태: `{status}`", "",
        "| " + " | ".join(display_columns) + " |",
        "|" + "|".join("---" for _ in display_columns) + "|",
    ]
    for row in safe_rows:
        lines.append("| " + " | ".join(str(row.get(column, "N/A")) if row.get(column) is not None else "N/A" for column in display_columns) + " |")
    lines += ["", "N/A는 양성 레이블 부재, 작은 표본에서 K=0, 또는 해당 지표의 정의 불가를 뜻하며 0으로 대체하지 않는다."]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "markdown": markdown_path}
