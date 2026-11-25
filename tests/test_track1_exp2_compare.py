import json

from evaluation.track1.exp2_compare import compare_exp2


def test_compare_freezes_analysis_before_reading_truth(tmp_path):
    tickets = tmp_path / "tickets.json"
    truth = tmp_path / "truth.json"
    output = tmp_path / "result"
    rows = [
        {"accountId": "a" * 64, "reservationNo": "1" * 64, "performanceId": "p1", "purchasedAt": "2026-01-01T00:00:00Z", "refundAccountHash": "e" * 64},
        {"accountId": "b" * 64, "reservationNo": "2" * 64, "performanceId": "p1", "purchasedAt": "2026-01-01T00:01:00Z", "refundAccountHash": "e" * 64},
    ]
    tickets.write_text(json.dumps(rows), encoding="utf-8")
    truth.write_text(json.dumps({"a" * 64: "SCALPER_RING", "b" * 64: "SCALPER_RING"}), encoding="utf-8")
    target = compare_exp2(tickets, truth, output)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["truthReadAfterAnalysis"] is True
    assert payload["status"] == "COMPLETED_OFFLINE_COMPONENT_PROXY"
    assert payload["algorithmStatus"] == {"componentProxy": "COMPLETED", "networkxLouvain": "NOT_RUN", "gdsLouvain": "NOT_RUN"}
    assert payload["exp2"]["metrics"]["precisionAt5"] == 1.0
    assert payload["exp2"]["metrics"]["groupRecallAt5"] == 1.0
    assert payload["exp2"]["metrics"]["pairwiseF1"] == 1.0
    assert len(payload["frozenAnalysisSha256"]) == 64
    assert payload["truthSha256"] == __import__("hashlib").sha256(truth.read_bytes()).hexdigest()
