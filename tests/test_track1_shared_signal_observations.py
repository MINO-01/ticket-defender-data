"""대량 공유 관찰 계약과 상대 허브 임계값을 합성 입력으로 검증합니다. 탐지 효과 검증이 아닙니다."""

import hashlib
import json
import typing
import unittest
from pathlib import Path

from schemas.dto import (
    IpNetworkType, Track1AnalysisRequest, Track1LimitAnalysisRequest, Track1SharedSignalObservation,
)
from services.track1_scoring import build_account_token_graph, is_hub, score_v2, shared_signal_observations

FIXTURES = Path(__file__).parent / "fixtures"
RAW = (FIXTURES / "track1_shared_signal_scenario.json").read_bytes()
MANIFEST = json.loads((FIXTURES / "track1_shared_signal_scenario.manifest.json").read_text(encoding="utf-8"))
PAYLOAD = json.loads(RAW)
CONTRACT = FIXTURES / "shared_signal_observations.contract.json"
# Frontend src/test/fixtures/shared-signal-observations.contract.json과 같은 값입니다.
CONTRACT_SHA256 = "cb3c8de3a2f362684e4a5ac5d673a812772d040d656fac003bf36d24535651d7"


def request() -> Track1LimitAnalysisRequest:
    return Track1LimitAnalysisRequest.model_validate(PAYLOAD)


def dumped():
    return [item.model_dump(by_alias=True) for item in shared_signal_observations(request())]


def find(count, signal="IP"):
    return next(item for item in dumped() if item["distinctAccountCount"] == count and item["signalType"] == signal)


def ticket(account, reservation, ip):
    return {"accountId": account, "reservationNo": reservation, "eventId": "event-v1", "ipHash": ip}


class SharedSignalObservationTests(unittest.TestCase):
    def test_fixture_is_isolated_synthetic_input(self):
        self.assertEqual(hashlib.sha256(RAW.replace(b"\r\n", b"\n")).hexdigest(), MANIFEST["requestSha256"])
        self.assertIs(MANIFEST["finalForImport"], False)

    def test_contract_fixture_matches_real_output_and_schema(self):
        raw = CONTRACT.read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CONTRACT_SHA256)
        contract = json.loads(raw)
        self.assertEqual(contract, dumped())
        for item in contract:
            Track1SharedSignalObservation.model_validate(item)

    def test_enums_stay_shared_with_frontend(self):
        self.assertEqual(
            set(typing.get_args(IpNetworkType)),
            {"MOBILE_CGNAT", "RESIDENTIAL", "CORPORATE_EDU", "DATACENTER_PROXY", "UNKNOWN"},
        )

    def test_r4_and_n2_are_kept_outside_the_graph(self):
        graph = build_account_token_graph(request().tickets)
        projected = {account.key[1] for account in graph.accounts}
        self.assertFalse(any(name.startswith(("r4-", "n2-")) for name in projected))
        for count, network in ((20, "CORPORATE_EDU"), (30, "MOBILE_CGNAT")):
            with self.subTest(count=count):
                item = find(count)
                self.assertTrue(item["excludedFromLouvain"])
                self.assertEqual(item["ipNetworkType"], network)
                self.assertEqual(item["corroboration"], {"paymentSharedAccountCount": 0, "deviceSharedAccountCount": 0})
                self.assertEqual(item["ticketQuantityStatus"], "VERIFIED")
                self.assertEqual(item["totalTicketQuantity"], count)

    def test_purchase_window_uses_only_verified_times(self):
        self.assertEqual(find(20)["purchaseWindowSeconds"], 19)

    def test_incomplete_scope_keeps_observation_without_quantity(self):
        item = find(9)
        self.assertEqual(item["ticketQuantityStatus"], "UNAVAILABLE")
        self.assertIsNone(item["totalTicketQuantity"])
        self.assertIsNone(item["purchaseWindowSeconds"])

    def test_network_type_is_never_inferred(self):
        self.assertEqual(find(9)["ipNetworkType"], "UNKNOWN")
        payload = json.loads(RAW)
        first = next(t for t in payload["tickets"] if t.get("ipNetworkType") == "CORPORATE_EDU")
        first["ipNetworkType"] = "RESIDENTIAL"
        conflicting = Track1LimitAnalysisRequest.model_validate(payload)
        item = next(x for x in shared_signal_observations(conflicting) if x.distinct_account_count == 20)
        self.assertEqual(item.ip_network_type, "UNKNOWN")

    def test_response_has_no_raw_token(self):
        text = json.dumps(dumped())
        for t in PAYLOAD["tickets"]:
            if t["ipHash"]:
                self.assertNotIn(t["ipHash"], text)

    def test_observation_id_changes_per_request(self):
        payload = json.loads(RAW)
        payload["requestId"] = "b136558c-2326-4430-a46d-0793494e9b49"
        other = shared_signal_observations(Track1LimitAnalysisRequest.model_validate(payload))
        self.assertFalse({x["observationId"] for x in dumped()} & {x.observation_id for x in other})

    def test_v1_request_is_event_scoped_and_quantity_unavailable(self):
        tickets = [ticket(f"a{n}", f"r{n}", "c" * 64) for n in range(9)]
        parsed = Track1AnalysisRequest.model_validate({"requestId": PAYLOAD["requestId"], "tickets": tickets})
        item = shared_signal_observations(parsed)[0]
        self.assertEqual((item.scope_type, item.scope_id), ("EVENT", "event-v1"))
        self.assertEqual(item.ticket_quantity_status, "UNAVAILABLE")
        self.assertIsNone(item.total_ticket_quantity)

    def test_below_minimum_is_not_observed(self):
        tickets = [ticket(f"a{n}", f"r{n}", "c" * 64) for n in range(8)]
        parsed = Track1AnalysisRequest.model_validate({"requestId": PAYLOAD["requestId"], "tickets": tickets})
        self.assertEqual(shared_signal_observations(parsed), [])


class RelativeHubThresholdTests(unittest.TestCase):
    def test_minimums(self):
        self.assertFalse(is_hub("ip_hash", 8, scope_accounts=100))
        self.assertTrue(is_hub("ip_hash", 9, scope_accounts=100))
        self.assertFalse(is_hub("address_hash", 6, scope_accounts=100))
        self.assertTrue(is_hub("address_hash", 7, scope_accounts=100))

    def test_relative_threshold_uses_ceiling_of_one_percent(self):
        self.assertFalse(is_hub("ip_hash", 10, scope_accounts=1001))
        self.assertTrue(is_hub("ip_hash", 11, scope_accounts=1001))
        self.assertTrue(is_hub("ip_hash", 10, scope_accounts=1000))

    def test_payment_and_device_are_never_hubs(self):
        self.assertFalse(is_hub("payment_hash", 100000, scope_accounts=100))
        self.assertFalse(is_hub("device_id_hash", 100000, scope_accounts=100))

    def test_observed_but_not_excluded_in_large_scope(self):
        tickets = [ticket(f"a{n}", f"r{n}", "c" * 64) for n in range(10)]
        tickets += [ticket(f"b{n}", f"s{n}", f"{n:064x}") for n in range(1991)]
        parsed = Track1AnalysisRequest.model_validate({"requestId": PAYLOAD["requestId"], "tickets": tickets})
        item = next(x for x in shared_signal_observations(parsed) if x.distinct_account_count == 10)
        self.assertFalse(item.excluded_from_louvain)
        self.assertEqual(len(build_account_token_graph(parsed.tickets).excluded_hubs), 0)

    def test_score_formula_is_unchanged(self):
        coverage = {"payment_hash": 1.0, "device_id_hash": 1.0, "address_hash": 1.0, "ip_hash": 1.0}
        self.assertEqual(score_v2(6, coverage), 1.0)


if __name__ == "__main__":
    unittest.main()
