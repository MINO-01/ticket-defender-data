"""계정당 2매 한도를 전제로 한 시연용 합성 예매 원장 v2를 생성합니다.

ledger.py는 고정된 v1의 생성기 해시에 포함되므로 수정하지 않고 재사용만 합니다.
1행은 유효 티켓 1매라는 합성 데이터 전제를 따릅니다. 이 모듈은 분석 결과를 읽지 않으며
군집 ID, Louvain 결과, 위험 점수를 만들지 않습니다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import random
import uuid

from evaluation.track1.tokens import hmac_token

from .ledger import (
    A3_FILE, ANSWER_FILE, DEFAULT_EVENT_REF, DEFAULT_SEED, EXPECTED_ROW_COUNT, MANIFEST_FILE,
    PERFORMANCE, README_FILE, ROWS_PER_ZONE, SEATS_FILE, SEATS_PER_ROW, TENANT_SCOPE, TRACK1_FILE,
    ZONES, Account, Dataset, Pattern, Seat, _REPO_ROOT, _a3_bytes, _answer_bytes, _git, _json_bytes,
    _pattern_definitions, _seats_bytes, _symbols, _take_group, _track1_bytes, normalize_performance_id,
    require_outside_repository, validate_event_ref,
)


SOURCE_VERSION_PREFIX = "capstone-demo-ledger-v2-seed"
DEMO_MAX_TICKETS_PER_ACCOUNT = 2
POLICY_AMBIGUOUS_CATEGORY = "POLICY_AMBIGUOUS_REVIEW"
QUANTITY_FIXTURE_FILE = "quantity_rule_fixture.json"
OUTPUT_FILES = (A3_FILE, TRACK1_FILE, ANSWER_FILE, SEATS_FILE, README_FILE, QUANTITY_FIXTURE_FILE)

_GENERATOR_SOURCES = (
    "evaluation/capstone_demo/ledger.py",
    "evaluation/capstone_demo/ledger_v2.py",
    "evaluation/track1/tokens.py",
)
_PARTY_SIZES = (1, 2)
_PARTY_WEIGHTS = (60, 40)


def source_version_for(seed: int) -> str:
    return f"{SOURCE_VERSION_PREFIX}{seed}"


def default_performance_id(seed: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ticket-defender:capstone-demo:performance:v2:{seed}"))


def _patterns_v2() -> tuple[Pattern, ...]:
    """v1 관계 패턴을 유지하고 N3만 정책상 불확실 사례로 분류합니다."""

    return tuple(
        replace(pattern, category=POLICY_AMBIGUOUS_CATEGORY) if pattern.pattern_id == "N3" else pattern
        for pattern in _pattern_definitions()
    )


def _partition_seats(rng: random.Random) -> list[list[Seat]]:
    groups: list[list[Seat]] = []
    for zone in ZONES:
        for row in range(1, ROWS_PER_ZONE + 1):
            seat = 1
            while seat <= SEATS_PER_ROW:
                size = min(rng.choices(_PARTY_SIZES, _PARTY_WEIGHTS)[0], SEATS_PER_ROW - seat + 1)
                groups.append([Seat(zone, str(row), str(s)) for s in range(seat, seat + size)])
                seat += size
    rng.shuffle(groups)
    return groups


def _build_accounts(rng: random.Random, patterns: tuple[Pattern, ...]) -> list[Account]:
    groups = _partition_seats(rng)
    accounts: list[Account] = []
    for pattern in patterns:
        for symbols in pattern.symbols:
            accounts.append(Account(_take_group(groups, pattern.min_party_size), dict(symbols), pattern.pattern_id))
    for index, group in enumerate(groups):
        symbols = _symbols("NORMAL", index, {})
        if rng.random() < 0.4:
            symbols["address_hash"] = None
        accounts.append(Account(group, symbols))

    account_numbers = rng.sample(range(10_000, 100_000), len(accounts))
    reservation_numbers = iter(rng.sample(range(100_000, 1_000_000), EXPECTED_ROW_COUNT))
    for account, number in zip(accounts, account_numbers):
        if len(account.seats) > DEMO_MAX_TICKETS_PER_ACCOUNT:
            raise ValueError("Ledger v2 account exceeds the demo purchase limit")
        account.account_id = f"SYNTH-ACCT-V2-{number}"
        account.reservation_nos = [
            f"SYNTHETIC-CAPSTONE-DEMO-V2-{next(reservation_numbers)}" for _ in account.seats
        ]
    return accounts


def quantity_rule_fixture(event_ref: str = DEFAULT_EVENT_REF) -> dict[str, object]:
    """수량 규칙 시험 전용 입력입니다. 960석 원장과 예매참조를 공유하지 않습니다."""

    event_id = f"{event_ref}-quantity-fixture"
    cases = (("Q-ONE", 1), ("Q-TWO", 2), ("Q-THREE", 3), ("Q-FOUR", 4))
    tickets = []
    for account, quantity in cases:
        for number in range(1, quantity + 1):
            tickets.append({
                "accountId": f"SYNTH-{account}", "reservationNo": f"SYNTHETIC-{account}-{number}",
                "eventId": event_id,
                "paymentHash": hmac_token(TENANT_SCOPE, "payment_hash", (f"quantity-{account}",)),
            })
    return {
        "kind": "QUANTITY_RULE_TEST_FIXTURE",
        "notPartOfLedger": True,
        "unit": "ONE_ROW_IS_ONE_VALID_TICKET_SYNTHETIC_ASSUMPTION",
        "tickets": tickets,
    }


def _readme_bytes(seed: int, source_version: str, performance_confirmed: bool) -> bytes:
    status = "확인된 공연 UUID로 생성" if performance_confirmed else "자리표시 공연 UUID, 최종 반입 금지"
    text = f"""# 캡스톤 시연 합성 예매 원장 v2

공연: {PERFORMANCE['displayName']}, {PERFORMANCE['performanceAt']}

{PERFORMANCE['disclaimer']} 모든 값은 seed {seed}로 만든 합성 데이터입니다.

- sourceVersion: {source_version}
- 공연 UUID 상태: {status}
- 시연 가정: 계정당 2매, 1행은 유효 티켓 1매
- {QUANTITY_FIXTURE_FILE}는 수량 규칙 시험 전용이며 원장에 포함되지 않습니다.
- 군집과 위험 점수는 이 파일에 없습니다. 위험 점수는 조사 우선순위이며 위법 확률이 아닙니다.
- {ANSWER_FILE}는 평가 전용이며 분석 입력, Backend, Frontend에 전달하지 않습니다.
"""
    return text.encode("utf-8")


def build_dataset(
    seed: int = DEFAULT_SEED, performance_id: str | None = None, event_ref: str = DEFAULT_EVENT_REF
) -> Dataset:
    """seed와 공연 참조만으로 v2 출력 바이트를 결정적으로 만듭니다."""

    performance_confirmed = performance_id is not None
    performance_id = default_performance_id(seed) if performance_id is None else normalize_performance_id(performance_id)
    event_ref = validate_event_ref(event_ref)
    source_version = source_version_for(seed)
    rng = random.Random(f"ledger-v2:{seed}")
    patterns = _patterns_v2()
    accounts = _build_accounts(rng, patterns)
    rows = sorted(
        ((reservation_no, seat) for account in accounts
         for reservation_no, seat in zip(account.reservation_nos, account.seats)),
        key=lambda item: item[0],
    )
    if len(rows) != EXPECTED_ROW_COUNT:
        raise ValueError("Unexpected ledger row count")
    canonical = "".join(f"{ref}\t{seat.zone}\t{seat.row}\t{seat.seat}\n" for ref, seat in rows)
    return Dataset(
        seed=seed,
        source_version=source_version,
        performance_id=performance_id,
        event_ref=event_ref,
        row_count=len(rows),
        rows_sha256=sha256(canonical.encode("ascii")).hexdigest(),
        files={
            A3_FILE: _a3_bytes(rows, performance_id, source_version),
            TRACK1_FILE: _track1_bytes(rng, accounts, event_ref),
            ANSWER_FILE: _answer_bytes(accounts, patterns),
            SEATS_FILE: _seats_bytes(seed, rows, source_version),
            README_FILE: _readme_bytes(seed, source_version, performance_confirmed),
            QUANTITY_FIXTURE_FILE: _json_bytes(quantity_rule_fixture(event_ref)),
        },
    )


def generator_code_sha256() -> str:
    digest = sha256()
    for relative in _GENERATOR_SOURCES:
        digest.update(relative.encode("ascii") + b"\0")
        digest.update((_REPO_ROOT / relative).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def generator_git_state() -> dict[str, object]:
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--", *_GENERATOR_SOURCES)
    return {"gitHeadSha": head, "generatorCommitted": head is not None and status == ""}


def build_manifest(
    dataset: Dataset, generated_at: str, git_state: dict[str, object], performance_confirmed: bool
) -> bytes:
    hashes = {name: sha256(content).hexdigest() for name, content in dataset.files.items()}
    return _json_bytes({
        "schemaVersion": 2,
        "datasetKind": "SYNTHETIC_RESERVATION_LEDGER_CAPSTONE_DEMO",
        "seed": dataset.seed,
        "sourceVersion": dataset.source_version,
        "performanceId": dataset.performance_id,
        "performanceIdConfirmed": performance_confirmed,
        "finalForImport": performance_confirmed,
        "eventRef": dataset.event_ref,
        "tenantScope": TENANT_SCOPE,
        "performance": PERFORMANCE,
        "demoPolicy": {
            "maxTicketsPerAccount": DEMO_MAX_TICKETS_PER_ACCOUNT,
            "status": "DEMO_ASSUMPTION_NOT_SERVER_VALIDATED_POLICY",
        },
        "quantityUnit": "ONE_ROW_IS_ONE_VALID_TICKET_SYNTHETIC_ASSUMPTION",
        "generatedAt": generated_at,
        "generator": {"codeSha256": generator_code_sha256(), **git_state},
        "ledger": {
            "rowCount": dataset.row_count,
            "a3FixtureSha256": hashes[A3_FILE],
            "rowsSha256": dataset.rows_sha256,
        },
        "track1InputSha256": hashes[TRACK1_FILE],
        "answerKeySha256": hashes[ANSWER_FILE],
        "files": {
            name: {"sha256": hashes[name], "bytes": len(content)}
            for name, content in dataset.files.items()
        },
        "independence": {
            "readsPostImages": False,
            "readsModelOutputs": False,
            "containsAnalysisResults": False,
            "externalModelCalls": 0,
        },
    })


def generate(
    output_dir: Path, seed: int = DEFAULT_SEED, performance_id: str | None = None,
    event_ref: str = DEFAULT_EVENT_REF, require_committed_generator: bool = True,
    allow_unconfirmed_performance: bool = False,
) -> Path:
    """새 디렉터리에만 기록합니다. 공연 UUID가 없으면 명시적 시험 옵션으로만 허용합니다."""

    if performance_id is None and not allow_unconfirmed_performance:
        raise ValueError("A confirmed performance_id is required before freezing ledger v2")
    target = require_outside_repository(output_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError("Output directory is not empty; a frozen ledger is never overwritten")
    git_state = generator_git_state()
    if require_committed_generator and not git_state["generatorCommitted"]:
        raise RuntimeError("Generator source must be committed before freezing a ledger")
    dataset = build_dataset(seed, performance_id, event_ref)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest = build_manifest(dataset, generated_at, git_state, performance_id is not None)
    target.mkdir(parents=True, exist_ok=True)
    for name, content in dataset.files.items():
        with (target / name).open("xb") as output:
            output.write(content)
    with (target / MANIFEST_FILE).open("xb") as output:
        output.write(manifest)
    return target / MANIFEST_FILE


def _limit_and_reference_checks(output_dir: Path) -> dict[str, bool]:
    tickets = json.loads((output_dir / TRACK1_FILE).read_text(encoding="utf-8"))["tickets"]
    counts: dict[str, int] = {}
    for ticket in tickets:
        counts[ticket["accountId"]] = counts.get(ticket["accountId"], 0) + 1
    a3_refs = {
        line.split("=", 1)[1]
        for line in (output_dir / A3_FILE).read_text(encoding="ascii").splitlines()
        if ".reservation-reference=" in line
    }
    return {
        "maxTwoTicketsPerAccount": max(counts.values()) <= DEMO_MAX_TICKETS_PER_ACCOUNT,
        "a3AndTrack1ReferencesMatch": a3_refs == {ticket["reservationNo"] for ticket in tickets},
    }


def verify(output_dir: Path, require_committed_generator: bool = True) -> dict[str, object]:
    """저장 파일이 manifest와 일치하고 같은 입력으로 재현되는지 검사합니다."""

    manifest_bytes = (output_dir / MANIFEST_FILE).read_bytes()
    manifest = json.loads(manifest_bytes)
    rebuilt = build_dataset(
        manifest["seed"],
        manifest["performanceId"] if manifest["performanceIdConfirmed"] else None,
        manifest["eventRef"],
    )
    checks = {
        "fileHashesMatchManifest": all(
            sha256((output_dir / name).read_bytes()).hexdigest() == meta["sha256"]
            for name, meta in manifest["files"].items()
        ),
        "fileSetMatchesManifest": set(manifest["files"]) == set(OUTPUT_FILES),
        "rowCountIs960": manifest["ledger"]["rowCount"] == EXPECTED_ROW_COUNT == rebuilt.row_count,
        "sourceVersionMatches": manifest["sourceVersion"] == rebuilt.source_version,
        "rowsHashReproduced": manifest["ledger"]["rowsSha256"] == rebuilt.rows_sha256,
        "bytesReproducedFromSeed": all(
            (output_dir / name).read_bytes() == content for name, content in rebuilt.files.items()
        ),
        "generatorCodeUnchanged": manifest["generator"]["codeSha256"] == generator_code_sha256(),
        "generatorWasCommitted": manifest["generator"]["generatorCommitted"] is True,
        **_limit_and_reference_checks(output_dir),
    }
    required = {
        name: passed for name, passed in checks.items()
        if require_committed_generator or name != "generatorWasCommitted"
    }
    return {
        "status": "PASS" if all(required.values()) else "FAIL",
        "manifestSha256": sha256(manifest_bytes).hexdigest(),
        "sourceVersion": manifest["sourceVersion"],
        "finalForImport": manifest["finalForImport"],
        "checks": checks,
    }
