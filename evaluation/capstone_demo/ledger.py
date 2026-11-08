"""고정 seed로 시연용 합성 예매 원장과 Track 1 입력을 생성합니다.

이 모듈은 게시물 이미지, 모델 출력, 분석 결과를 입력으로 받지 않습니다.
R1~R3, N1~N3는 생성 시 주입한 관계 패턴과 평가용 정답일 뿐 분석 결과가 아니며,
Louvain 군집과 위험 점수는 실제 Spring, FastAPI, Neo4j GDS 경로에서만 계산됩니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import random
import subprocess
import uuid

from evaluation.track1.tokens import hmac_token


DEFAULT_SEED = 20261108
ZONES = ("19", "20", "21")
ROWS_PER_ZONE = 20
SEATS_PER_ROW = 16
EXPECTED_ROW_COUNT = len(ZONES) * ROWS_PER_ZONE * SEATS_PER_ROW

FIXTURE_TYPE = "A3_SYNTHETIC_RESERVATION_SNAPSHOT_V1"
SOURCE_VERSION_PREFIX = "capstone-demo-ledger-v1-seed"
TENANT_SCOPE = "tenant-synthetic-capstone-demo"
DEFAULT_EVENT_REF = "capstone-demo-akmu-daejeon-20261205"
PERFORMANCE = {
    "displayName": "악동뮤지션 대전 콘서트 (시연)",
    "performanceAt": "2026-12-05T18:00:00+09:00",
    "venue": "대전 공연장 (시연 설정)",
    "ticketingPlatform": "가상 예매처 A",
    "disclaimer": "실제 공연의 공식 일정이나 예매 원장이 아니라 시연을 위해 가정한 설정입니다.",
}

A3_FILE = "a3_fixture.properties"
TRACK1_FILE = "track1_ingest.json"
ANSWER_FILE = "answer_key.json"
SEATS_FILE = "demo_seats.json"
MANIFEST_FILE = "dataset_manifest.json"
README_FILE = "README.md"
OUTPUT_FILES = (A3_FILE, TRACK1_FILE, ANSWER_FILE, SEATS_FILE, README_FILE)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR_SOURCES = (
    "evaluation/capstone_demo/ledger.py",
    "evaluation/track1/tokens.py",
)
_SIGNALS = ("payment_hash", "address_hash", "device_id_hash", "ip_hash")
_JSON_SIGNAL_KEYS = {
    "payment_hash": "paymentHash",
    "address_hash": "addressHash",
    "device_id_hash": "deviceIdHash",
    "ip_hash": "ipHash",
}
_PARTY_SIZES = (1, 2, 3, 4)
_PARTY_WEIGHTS = (60, 30, 6, 4)
_DEMO_SEAT_CANDIDATES = 10


@dataclass(frozen=True)
class Seat:
    zone: str
    row: str
    seat: str


@dataclass
class Account:
    """생성 중에만 쓰는 계정입니다. 기호 신호값은 HMAC 토큰으로만 출력됩니다."""

    seats: list[Seat]
    symbols: dict[str, str | None]
    pattern_id: str | None = None
    account_id: str = ""
    reservation_nos: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    category: str
    description: str
    shared_signals: tuple[str, ...]
    symbols: tuple[dict[str, str | None], ...]
    min_party_size: int


@dataclass(frozen=True)
class Dataset:
    seed: int
    source_version: str
    performance_id: str
    event_ref: str
    row_count: int
    rows_sha256: str
    files: dict[str, bytes]


def _unique(pattern_id: str, index: int, signal: str) -> str:
    return f"{pattern_id}-{index}-{signal}-unique"


def _symbols(pattern_id: str, index: int, shared: dict[str, str | None]) -> dict[str, str | None]:
    """공유값을 지정하지 않은 신호는 계정별 고유값으로 채웁니다."""

    return {
        signal: shared[signal] if signal in shared else _unique(pattern_id, index, signal)
        for signal in _SIGNALS
    }


def _pattern_definitions() -> tuple[Pattern, ...]:
    r1 = tuple(
        _symbols("R1", index, {
            "payment_hash": "r1-pay", "address_hash": "r1-addr",
            "device_id_hash": "r1-dev", "ip_hash": "r1-ip",
        })
        for index in range(6)
    )
    r2_links = (
        {"payment_hash": "r2-pay-a"},
        {"payment_hash": "r2-pay-a", "device_id_hash": "r2-dev-a"},
        {"device_id_hash": "r2-dev-a", "payment_hash": "r2-pay-b"},
        {"payment_hash": "r2-pay-b", "device_id_hash": "r2-dev-b"},
        {"device_id_hash": "r2-dev-b", "payment_hash": "r2-pay-c"},
        {"payment_hash": "r2-pay-c"},
    )
    r3_links = (
        {"payment_hash": "r3-pay", "ip_hash": "r3-ip-a"},
        {"payment_hash": "r3-pay", "device_id_hash": "r3-dev"},
        {"address_hash": "r3-addr", "device_id_hash": "r3-dev"},
        {"address_hash": "r3-addr", "device_id_hash": "r3-dev", "ip_hash": "r3-ip-b"},
        {"device_id_hash": "r3-dev", "ip_hash": "r3-ip-a"},
    )
    return (
        Pattern("R1", "INJECTED_INVESTIGATION_RELATIONSHIP", "네 신호를 모두 공유하는 밀집 관계",
                ("payment_hash", "address_hash", "device_id_hash", "ip_hash"), r1, 2),
        Pattern("R2", "INJECTED_INVESTIGATION_RELATIONSHIP", "전체 공통 신호 없이 이어지는 다단계 연결",
                ("payment_hash", "device_id_hash"),
                tuple(_symbols("R2", i, links) for i, links in enumerate(r2_links)), 2),
        Pattern("R3", "INJECTED_INVESTIGATION_RELATIONSHIP", "약한 신호가 겹쳐 형성된 복합 연결",
                ("payment_hash", "address_hash", "device_id_hash", "ip_hash"),
                tuple(_symbols("R3", i, links) for i, links in enumerate(r3_links)), 2),
        Pattern("N1", "NORMAL_SHARED_COUNTEREXAMPLE", "주소와 IP를 공유하는 동거 가족",
                ("address_hash", "ip_hash"),
                tuple(_symbols("N1", i, {"address_hash": "n1-addr", "ip_hash": "n1-ip"}) for i in range(4)), 1),
        Pattern("N2", "NORMAL_SHARED_COUNTEREXAMPLE", "공용 네트워크 IP만 공유하는 독립 예매자",
                ("ip_hash",),
                tuple(_symbols("N2", i, {"ip_hash": "n2-ip", "address_hash": None}) for i in range(30)), 1),
        Pattern("N3", "NORMAL_SHARED_COUNTEREXAMPLE", "주최자 한 명의 결제수단으로 대리 결제한 단체 관람",
                ("payment_hash",),
                tuple(_symbols("N3", i, {"payment_hash": "n3-pay", "address_hash": None}) for i in range(6)), 1),
    )


def _partition_seats(rng: random.Random) -> list[list[Seat]]:
    """모든 좌석을 같은 열의 연속 좌석 묶음으로 나눕니다."""

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


def _take_group(groups: list[list[Seat]], min_size: int) -> list[Seat]:
    for index, group in enumerate(groups):
        if len(group) >= min_size:
            return groups.pop(index)
    raise ValueError("Not enough seat groups for the relationship patterns")


def _build_accounts(rng: random.Random, patterns: tuple[Pattern, ...]) -> list[Account]:
    groups = _partition_seats(rng)
    accounts: list[Account] = []
    for pattern in patterns:
        for symbols in pattern.symbols:
            accounts.append(Account(_take_group(groups, pattern.min_party_size), dict(symbols), pattern.pattern_id))
    for index, group in enumerate(groups):
        symbols = _symbols("NORMAL", index, {})
        # 모바일 티켓 예매처럼 배송지가 없는 정상 예매도 포함합니다.
        if rng.random() < 0.4:
            symbols["address_hash"] = None
        accounts.append(Account(group, symbols))

    account_numbers = rng.sample(range(10_000, 100_000), len(accounts))
    reservation_numbers = iter(rng.sample(range(100_000, 1_000_000), EXPECTED_ROW_COUNT))
    for account, number in zip(accounts, account_numbers):
        account.account_id = f"SYNTH-ACCT-{number}"
        account.reservation_nos = [
            f"SYNTHETIC-CAPSTONE-DEMO-{next(reservation_numbers)}" for _ in account.seats
        ]
    return accounts


def _token(signal: str, symbol: str | None) -> str | None:
    return None if symbol is None else hmac_token(TENANT_SCOPE, signal, (symbol,))


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _a3_bytes(rows: list[tuple[str, Seat]], performance_id: str, source_version: str) -> bytes:
    lines = [
        f"fixture-type={FIXTURE_TYPE}",
        "complete-snapshot=true",
        f"performance-id={performance_id}",
        f"source-version={source_version}",
        f"row-count={len(rows)}",
    ]
    for index, (reference, seat) in enumerate(rows):
        lines.extend((
            f"row.{index}.reservation-reference={reference}",
            f"row.{index}.zone={seat.zone}",
            f"row.{index}.row={seat.row}",
            f"row.{index}.seat={seat.seat}",
        ))
    return ("\n".join(lines) + "\n").encode("ascii")


def _track1_bytes(rng: random.Random, accounts: list[Account], event_ref: str) -> bytes:
    """현재 Spring AgentAnalysisRequest 필드만 출력합니다."""

    tickets = []
    for account in accounts:
        for reservation_no in account.reservation_nos:
            ticket = {"accountId": account.account_id, "reservationNo": reservation_no, "eventId": event_ref}
            for signal in _SIGNALS:
                token = _token(signal, account.symbols[signal])
                if token is not None:
                    ticket[_JSON_SIGNAL_KEYS[signal]] = token
            tickets.append(ticket)
    rng.shuffle(tickets)
    return _json_bytes({"tickets": tickets})


def _answer_bytes(accounts: list[Account], patterns: tuple[Pattern, ...]) -> bytes:
    by_pattern = [
        {
            "patternId": pattern.pattern_id,
            "category": pattern.category,
            "description": pattern.description,
            "sharedSignals": [_JSON_SIGNAL_KEYS[signal] for signal in pattern.shared_signals],
            "members": sorted(
                ({"accountId": account.account_id, "reservationNos": sorted(account.reservation_nos)}
                 for account in accounts if account.pattern_id == pattern.pattern_id),
                key=lambda member: member["accountId"],
            ),
        }
        for pattern in patterns
    ]
    independent = [account for account in accounts if account.pattern_id is None]
    return _json_bytes({
        "kind": "INJECTED_RELATIONSHIP_TRUTH_FOR_EVALUATION_ONLY",
        "isAnalysisResult": False,
        "loadIntoBackendOrFrontend": False,
        "patterns": by_pattern,
        "independentNormalAccounts": {
            "accountCount": len(independent),
            "multiSeatAccountCount": sum(1 for account in independent if len(account.seats) > 1),
            "note": "한 계정의 연속 좌석은 같은 신호를 공유하며 관계 패턴 정답이 아닌 정상 동반 예매입니다.",
        },
    })


def _seats_bytes(seed: int, rows: list[tuple[str, Seat]], source_version: str) -> bytes:
    """원장 고정 뒤 A 기능 시험에 고를 수 있는 좌석 후보만 출력합니다."""

    picked = random.Random(f"{seed}:demo-seat-candidates").sample(rows, _DEMO_SEAT_CANDIDATES)
    return _json_bytes({
        "performance": PERFORMANCE,
        "sourceVersion": source_version,
        "purpose": "FUNCTION_TEST_SEAT_SELECTION_NOT_DETECTION_ACCURACY",
        "note": "기능 시험용 좌석 선택 후보이며 탐지 정확도 실험의 표본이 아닙니다.",
        "candidates": [
            {"reservationReference": reference, "zone": seat.zone, "row": seat.row, "seat": seat.seat}
            for reference, seat in sorted(picked, key=lambda item: item[0])
        ],
    })


def _readme_bytes(seed: int, source_version: str) -> bytes:
    text = f"""# 캡스톤 시연 합성 예매 원장

공연: {PERFORMANCE['displayName']}, {PERFORMANCE['performanceAt']}, {PERFORMANCE['venue']}, {PERFORMANCE['ticketingPlatform']}

{PERFORMANCE['disclaimer']} 모든 값은 seed {seed}로 만든 합성 데이터이며 실제 예매처의 데이터 제공 승인이나 API 접근 권한을 뜻하지 않습니다.

| 파일 | 용도 |
| --- | --- |
| {A3_FILE} | A3 합성 반입 fixture, sourceVersion {source_version} |
| {TRACK1_FILE} | 현재 Spring Track 1 수집 DTO 형식의 입력, HMAC 토큰만 포함 |
| {ANSWER_FILE} | 생성 시 주입한 관계 패턴 정답, 평가 전용이며 Backend와 Frontend에 전달 금지 |
| {SEATS_FILE} | 원장 고정 뒤 기능 시험 좌석을 고를 때 쓰는 후보 |
| {MANIFEST_FILE} | seed, 생성기 코드 SHA, 파일 SHA-256, 행 수, 생성 시각 |

## 고정 규칙

- 이 디렉터리의 파일은 게시물 이미지, 모델 출력, 분석 결과 없이 생성되었습니다.
- 시연 게시물 작성 전에 고정하며, 결과에 맞춰 좌석, 정답, 입력 토큰을 바꾸지 않습니다.
- 군집과 위험 점수는 이 파일에 없습니다. 분석은 기존 Spring, FastAPI, Neo4j GDS Louvain 경로에서만 수행합니다.
- 위험 점수는 조사 우선순위이며 위법 확률이 아닙니다. MATCH_CANDIDATE와 FOUND는 확정 위반이 아닙니다.
"""
    return text.encode("utf-8")


def source_version_for(seed: int) -> str:
    return f"{SOURCE_VERSION_PREFIX}{seed}"


def default_performance_id(seed: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ticket-defender:capstone-demo:performance:{seed}"))


def build_dataset(
    seed: int = DEFAULT_SEED, performance_id: str | None = None, event_ref: str = DEFAULT_EVENT_REF
) -> Dataset:
    """seed와 공연 참조만으로 출력 바이트를 결정적으로 만듭니다."""

    performance_id = performance_id or default_performance_id(seed)
    source_version = source_version_for(seed)
    rng = random.Random(seed)
    patterns = _pattern_definitions()
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
            README_FILE: _readme_bytes(seed, source_version),
        },
    )


def generator_code_sha256() -> str:
    """체크아웃 줄바꿈 차이에 영향받지 않도록 LF로 정규화해 해시합니다."""

    digest = sha256()
    for relative in _GENERATOR_SOURCES:
        digest.update(relative.encode("ascii") + b"\0")
        digest.update((_REPO_ROOT / relative).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ("git", *args), cwd=_REPO_ROOT, capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def generator_git_state() -> dict[str, object]:
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--", *_GENERATOR_SOURCES)
    return {"gitHeadSha": head, "generatorCommitted": head is not None and status == ""}


def require_outside_repository(output_dir: Path) -> Path:
    """결과 경로가 어떤 Git 저장소 안에도 있지 않은지 확인합니다."""

    resolved = output_dir.resolve()
    if resolved.is_relative_to(_REPO_ROOT) or any(
        (candidate / ".git").exists() for candidate in (resolved, *resolved.parents)
    ):
        raise ValueError("Output directory must be outside every Git repository")
    return resolved


def build_manifest(dataset: Dataset, generated_at: str, git_state: dict[str, object]) -> bytes:
    hashes = {name: sha256(content).hexdigest() for name, content in dataset.files.items()}
    return _json_bytes({
        "schemaVersion": 1,
        "datasetKind": "SYNTHETIC_RESERVATION_LEDGER_CAPSTONE_DEMO",
        "seed": dataset.seed,
        "sourceVersion": dataset.source_version,
        "performanceId": dataset.performance_id,
        "eventRef": dataset.event_ref,
        "tenantScope": TENANT_SCOPE,
        "performance": PERFORMANCE,
        "generatedAt": generated_at,
        "generator": {"codeSha256": generator_code_sha256(), **git_state},
        "ledger": {
            "rowCount": dataset.row_count,
            "a3FixtureSha256": hashes[A3_FILE],
            "rowsSha256": dataset.rows_sha256,
        },
        "track1InputSha256": hashes[TRACK1_FILE],
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
) -> Path:
    """새 디렉터리에만 원장을 기록합니다. 기존 고정 원장은 덮어쓰지 않습니다."""

    target = require_outside_repository(output_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError("Output directory is not empty; a frozen ledger is never overwritten")
    git_state = generator_git_state()
    if require_committed_generator and not git_state["generatorCommitted"]:
        raise RuntimeError("Generator source must be committed before freezing a ledger")
    dataset = build_dataset(seed, performance_id, event_ref)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest = build_manifest(dataset, generated_at, git_state)
    target.mkdir(parents=True, exist_ok=True)
    for name, content in dataset.files.items():
        with (target / name).open("xb") as output:
            output.write(content)
    with (target / MANIFEST_FILE).open("xb") as output:
        output.write(manifest)
    return target / MANIFEST_FILE


def verify(output_dir: Path) -> dict[str, object]:
    """저장 파일이 manifest와 일치하고 같은 입력으로 다시 재현되는지 검사합니다."""

    manifest_bytes = (output_dir / MANIFEST_FILE).read_bytes()
    manifest = json.loads(manifest_bytes)
    rebuilt = build_dataset(manifest["seed"], manifest["performanceId"], manifest["eventRef"])
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
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "manifestSha256": sha256(manifest_bytes).hexdigest(),
        "sourceVersion": manifest["sourceVersion"],
        "checks": checks,
    }
