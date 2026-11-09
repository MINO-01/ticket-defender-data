"""촬영용 합성 원장과 listing_A/B/C 이미지를 demo/capture-akmu-v1에 만들고 무결성을 검증합니다.

    .\\venv\\Scripts\\python.exe -B scripts\\generate_capture_demo.py          (생성)
    .\\venv\\Scripts\\python.exe -B scripts\\generate_capture_demo.py --verify (반입 전 검증)

모든 값은 합성입니다. 공연, 계정, 좌석은 가상이며 run960과 기존 fixture를 읽거나 바꾸지 않습니다.
tenant, event, performance UUID는 파일에 넣지 않고 격리 환경에서 확정한 값을 화면에서 선택합니다.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "demo" / "capture-akmu-v1"
MANIFEST = "manifest.json"
FONT_REGULAR = r"C:\Windows\Fonts\malgun.ttf"
FONT_BOLD = r"C:\Windows\Fonts\malgunbd.ttf"

SOURCE_VERSION = "akmu-demo-ledger-v1"
RESERVATION_SOURCE_VERSION = "akmu-demo-reservations-v1"
SOURCE_AS_OF = "2026-11-08T10:00:00Z"
QUANTITY_BASIS = "SYNTHETIC_PROVIDER_TICKET_LEDGER"
EVENT_NAME = "악동뮤지션 대전 콘서트 (합성 시연)"
PERFORMANCE_TEXT = "2026년 12월 5일 18:00"
PERFORMANCE_AT = "2026-12-05T18:00:00+09:00"
VENUE = "대전 공연장 (가상)"
ZONES = ("19", "20", "21")
ROWS_PER_ZONE = 20
SEATS_PER_ROW = 16
PERFORMANCE_REF = "AKMU-DAEJEON-20261205-1800"
PLATFORM = "합성중고장터"
LINKED_RESERVATION = "akmu-r2-res-3"
LISTING_A_SEAT = ("19", "8", "12")
LISTING_B_SEAT = ("21", "20", "16")


def _row(account, reservation, line, tokens, purchased="2026-11-02T10:00:00Z", quantity=1, unit="TICKET", status="ACTIVE"):
    return {
        "account": account, "reservation": reservation, "line": line, "purchased": purchased, "quantity": quantity,
        "unit": unit, "status": status, "payment": tokens.get("payment", ""), "address": tokens.get("address", ""),
        "device": tokens.get("device", ""), "ip": tokens.get("ip", ""),
    }


def build_rows() -> list[dict]:
    rows = []
    for n in range(1, 7):
        rows.append(_row(f"akmu-r1-{n}", f"akmu-r1-res-{n}", f"akmu-r1-line-{n}", {"payment": "akmu-r1-pay", "device": "akmu-r1-device"}))
    # R2는 결제와 기기가 사슬로 이어집니다. 실제 Louvain은 2개 커뮤니티로 분할한 실측이 있으며 입력으로 맞추지 않습니다.
    chain = [{"payment": "akmu-r2-pay-1"}, {"payment": "akmu-r2-pay-1", "device": "akmu-r2-device-1"},
             {"payment": "akmu-r2-pay-2", "device": "akmu-r2-device-1"}, {"payment": "akmu-r2-pay-2", "device": "akmu-r2-device-2"},
             {"device": "akmu-r2-device-2"}]
    for n, tokens in enumerate(chain, start=1):
        rows.append(_row(f"akmu-r2-{n}", f"akmu-r2-res-{n}", f"akmu-r2-line-{n}", tokens))
    for n in range(1, 31):
        rows.append(_row(f"akmu-n2-{n:02d}", f"akmu-n2-res-{n}", f"akmu-n2-line-{n}", {"ip": "akmu-n2-cgnat"},
                         purchased=f"2026-11-02T1{n % 2}:{n:02d}:00Z"))
    for n in range(1, 21):
        rows.append(_row(f"akmu-r4-{n:02d}", f"akmu-r4-res-{n}", f"akmu-r4-line-{n}", {"ip": "akmu-r4-org"},
                         purchased=f"2026-11-02T10:00:{n:02d}Z"))
    rows.append(_row("akmu-normal-1", "akmu-normal-res-1", "akmu-normal-line-1", {"payment": "akmu-normal-pay-1"}))
    for seat in (1, 2):
        rows.append(_row("akmu-within", "akmu-within-res", f"akmu-within-line-{seat}", {"payment": "akmu-within-pay"}))
    for seat in (1, 2, 3):
        rows.append(_row("akmu-over", "akmu-over-res", f"akmu-over-line-{seat}", {"payment": "akmu-over-pay"}))
    return rows


def assign_seats(rows: list[dict]) -> list[dict]:
    """통합 사례는 listing_A 좌석에 고정하고 나머지는 행 순서로 배정합니다. listing_B 좌석은 쓰지 않습니다.

    이 배정은 분석 결과를 보기 전에 정했습니다. run960과 고정 960석 원장을 읽거나 바꾸지 않습니다.
    """

    free = (
        (zone, str(row), str(seat)) for zone in ZONES for row in range(1, ROWS_PER_ZONE + 1) for seat in range(1, SEATS_PER_ROW + 1)
        if (zone, str(row), str(seat)) not in {LISTING_A_SEAT, LISTING_B_SEAT}
    )
    snapshot = []
    for row in rows:
        zone, row_no, seat = LISTING_A_SEAT if row["reservation"] == LINKED_RESERVATION else next(free)
        snapshot.append({"reservation": row["reservation"], "zone": zone, "row": row_no, "seat": seat, "line": row["line"]})
    return snapshot


def csv_body(lines: list[list[str]]) -> bytes:
    return ("".join(",".join(str(value) for value in line) + "\n" for line in lines)).encode("utf-8")


def ledger_csv(rows: list[dict]) -> bytes:
    return csv_body([[r["account"], r["reservation"], r["line"], r["purchased"], r["quantity"], r["unit"], r["status"], r["payment"], r["address"], r["device"], r["ip"]] for r in rows])


def snapshot_csv(snapshot: list[dict]) -> bytes:
    return csv_body([[s["reservation"], s["zone"], s["row"], s["seat"], s["line"]] for s in snapshot])


def draw_listing(path: Path, title: str, lines: list[tuple[str, str]], body: list[str], masked: set[str]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 800), (250, 250, 247))
    draw = ImageDraw.Draw(image)
    heading = ImageFont.truetype(FONT_BOLD, 34)
    label = ImageFont.truetype(FONT_REGULAR, 28)
    small = ImageFont.truetype(FONT_REGULAR, 22)
    draw.rectangle((0, 0, 900, 86), fill=(39, 133, 230))
    draw.text((32, 22), f"{PLATFORM} 판매글", font=heading, fill="white")
    if title:
        draw.text((32, 112), title, font=heading, fill=(20, 30, 45))
    y = 186
    for name, value in lines:
        draw.text((32, y), f"{name}:", font=label, fill=(90, 100, 115))
        if name in masked:
            draw.rectangle((190, y + 2, 400, y + 38), fill=(15, 15, 15))
        else:
            draw.text((190, y), value, font=label, fill=(20, 30, 45))
        y += 52
    y += 10
    draw.line((32, y, 868, y), fill=(200, 205, 215), width=2)
    y += 20
    for text in body:
        draw.text((32, y), text, font=label, fill=(45, 55, 70))
        y += 46
    draw.text((32, 752), "시연용 합성 이미지입니다. 실제 공연이나 판매가 아닙니다.", font=small, fill=(130, 135, 145))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def build() -> None:
    rows = build_rows()
    snapshot = assign_seats(rows)
    linked = next(s for s in snapshot if s["reservation"] == LINKED_RESERVATION)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "quantity_ledger.csv").write_bytes(ledger_csv(rows))
    (OUTPUT / "reservation_snapshot.csv").write_bytes(snapshot_csv(snapshot))
    common = [("공연", EVENT_NAME), ("회차", PERFORMANCE_TEXT), ("장소", VENUE)]
    images = OUTPUT / "images"
    a_seat = (linked["zone"], linked["row"], linked["seat"])
    if a_seat != LISTING_A_SEAT:
        raise ValueError("linked reservation must hold the listing_A seat")
    draw_listing(images / "listing_A.png", "", [*common, ("구역", a_seat[0]), ("열", a_seat[1]), ("좌석", a_seat[2]), ("판매가", "200,000원")],
                 ["일정이 안 맞아서 1장 양도합니다."], set())
    draw_listing(images / "listing_B.png", "", [*common, ("구역", LISTING_B_SEAT[0]), ("열", LISTING_B_SEAT[1]), ("좌석", LISTING_B_SEAT[2]), ("판매가", "180,000원")],
                 ["친구가 못 가게 되어 내놓습니다."], set())
    draw_listing(images / "listing_C.png", "", [*common, ("구역", ""), ("열", ""), ("좌석", ""), ("판매가", "190,000원")],
                 ["좌석 정보는 가렸으니 문의해 주세요."], {"구역", "열", "좌석"})

    tracked = ["quantity_ledger.csv", "reservation_snapshot.csv", "images/listing_A.png", "images/listing_B.png", "images/listing_C.png"]
    manifest = {
        "fixtureVersion": "akmu-capture-demo-v1", "synthetic": True, "finalForImport": False,
        "purpose": "격리 합성 환경의 촬영용 자료입니다. run960과 무관하며 실제 인물, 판매글, 개인정보가 없습니다.",
        "generator": "scripts/generate_capture_demo.py", "font": "malgun.ttf (Windows 시스템 글꼴, 저장소 미포함)",
        "import": {"sourceVersion": SOURCE_VERSION, "sourceAsOf": SOURCE_AS_OF, "quantityBasis": QUANTITY_BASIS, "completeSnapshot": True, "inputType": "INDIVIDUAL_TICKET", "reservationSourceVersion": RESERVATION_SOURCE_VERSION},
        "performance": {"eventName": EVENT_NAME, "performanceText": PERFORMANCE_TEXT, "performanceAt": PERFORMANCE_AT, "venue": VENUE, "zones": list(ZONES), "performanceRef": PERFORMANCE_REF},
        "policyToRegister": {"maxTicketsPerAccount": 2, "policyVersion": "akmu-demo-policy-r1", "effectiveFrom": "2026-11-01T00:00:00", "limitSubject": "ACCOUNT", "applicationScope": "PERFORMANCE", "cancellationRefundRule": "NETTED", "includesPresale": False},
        "rowCounts": {"quantityLedger": len(rows), "reservationSnapshot": len(snapshot)},
        "linkedCase": {"reservationReference": LINKED_RESERVATION, "account": "akmu-r2-3", "seat": dict(zip(("zone", "row", "seat"), a_seat))},
        "humanTruth": {
            "listing_A": {"zone": a_seat[0], "row": a_seat[1], "seat": a_seat[2], "askingPrice": 200000, "targetCandidateResult": "MATCH_CANDIDATE"},
            "listing_B": {"zone": LISTING_B_SEAT[0], "row": LISTING_B_SEAT[1], "seat": LISTING_B_SEAT[2], "askingPrice": 180000, "targetCandidateResult": "NO_MATCH"},
            "listing_C": {"zone": None, "row": None, "seat": None, "askingPrice": 190000, "targetCandidateResult": "UNKNOWN_OR_UNVERIFIABLE"},
        },
        "notes": ["targetCandidateResult는 목표이며 결과가 아닙니다. 실제 결과는 모델과 Backend가 결정합니다.",
                  "고정 960석 원장의 19구역 8열 12번은 R2 패턴이 아니어서 그 원장을 쓰지 않고 이 시연 전용 원장을 별도로 고정했습니다.",
                  "R2는 실측에서 2개 커뮤니티로 분할되었습니다. akmu-r2-3이 속한 군집을 Case 3에 사용합니다.",
                  "Backend 수량 원장에는 네트워크 유형 입력이 없어 R4, N2의 ipNetworkType은 화면에서 미확인으로 표시됩니다."],
        "files": {name: hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest() for name in tracked},
    }
    (OUTPUT / MANIFEST).write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"generated {len(rows)} ledger rows, {len(snapshot)} seats")


def _digest(path: Path) -> str:
    raw = path.read_bytes()
    # Git 줄바꿈 변환과 무관하게 같은 값을 얻도록 텍스트는 LF로 맞춥니다.
    return hashlib.sha256(raw.replace(b"\r\n", b"\n") if path.suffix == ".csv" else raw).hexdigest()


def verify(directory: Path = OUTPUT) -> list[str]:
    """manifest의 SHA-256과 내용 규칙을 검사하고 문제 목록을 돌려줍니다."""

    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    problems = [f"SHA_MISMATCH {name}" for name, expected in manifest["files"].items() if _digest(directory / name) != expected]
    ledger = [line.split(",") for line in (directory / "quantity_ledger.csv").read_text(encoding="utf-8").splitlines() if line]
    seats = [line.split(",") for line in (directory / "reservation_snapshot.csv").read_text(encoding="utf-8").splitlines() if line]
    if len(ledger) != manifest["rowCounts"]["quantityLedger"] or len(seats) != manifest["rowCounts"]["reservationSnapshot"]:
        problems.append("ROW_COUNT_MISMATCH")
    if any(len(row) != 11 or not any(row[7:]) for row in ledger):
        problems.append("LEDGER_ROW_INVALID")
    if len({row[2] for row in ledger}) != len(ledger):
        problems.append("DUPLICATE_TICKET_REFERENCE")
    if {row[1] for row in ledger} != {row[0] for row in seats}:
        problems.append("RESERVATION_SET_MISMATCH")
    seat_set = {tuple(row[1:4]) for row in seats}
    if len(seat_set) != len(seats):
        problems.append("DUPLICATE_SEAT")
    truth = manifest["humanTruth"]
    if (truth["listing_A"]["zone"], truth["listing_A"]["row"], truth["listing_A"]["seat"]) not in seat_set:
        problems.append("LISTING_A_SEAT_NOT_IN_SNAPSHOT")
    if (truth["listing_B"]["zone"], truth["listing_B"]["row"], truth["listing_B"]["seat"]) in seat_set:
        problems.append("LISTING_B_SEAT_IN_SNAPSHOT")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="촬영용 합성 자료 생성과 검증")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if not args.verify:
        build()
    problems = verify()
    print("VERIFY_PASS" if not problems else "\n".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
