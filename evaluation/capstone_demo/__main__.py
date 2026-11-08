"""시연 원장 생성, 고정 검증, 캡처 평가 템플릿 작성 명령입니다."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

from .capture_template import write_template
from .ledger import DEFAULT_EVENT_REF, DEFAULT_SEED, generate, verify


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evaluation.capstone_demo")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("generate", help="새 디렉터리에 합성 원장을 고정합니다")
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--seed", type=int, default=DEFAULT_SEED)
    create.add_argument("--performance-id", help="A3 격리 DB의 ACTIVE 공연 UUID")
    create.add_argument("--event-ref", default=DEFAULT_EVENT_REF)
    create.add_argument("--allow-uncommitted-generator", action="store_true", help="테스트 전용입니다")
    check = commands.add_parser("verify", help="고정 원장을 검증합니다")
    check.add_argument("--ledger-dir", type=Path, required=True)
    template = commands.add_parser("capture-template", help="빈 캡처 평가 템플릿을 작성합니다")
    template.add_argument("--ledger-dir", type=Path, required=True)
    template.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        manifest = generate(
            args.output_dir, args.seed, args.performance_id, args.event_ref,
            require_committed_generator=not args.allow_uncommitted_generator,
        )
        print(json.dumps({"manifest": str(manifest), "manifestSha256": sha256(manifest.read_bytes()).hexdigest()}))
        return 0
    if args.command == "verify":
        result = verify(args.ledger_dir)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "PASS" else 1
    print(json.dumps({"template": str(write_template(args.ledger_dir, args.output_dir))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
