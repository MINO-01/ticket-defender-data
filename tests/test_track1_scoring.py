"""계정 단위 그래프, score-v2, 수량 규칙의 순수 함수 테스트입니다. 합성 값만 사용합니다."""

import unittest

from evaluation.capstone_demo.compare import proxy_v2_clusters
from schemas.dto import Track1AnalysisRequest
from services.track1_scoring import (
    DEFAULT_PARAMETERS, QUANTITY_EVALUATED, QUANTITY_NOT_EVALUATED, REASON_POLICY_MISSING,
    REASON_QUANTITY_INCOMPLETE, GraphResultInvalid, PurchaseLimitPolicy, ScoreParameters,
    aggregate_accounts, build_account_token_graph, clusters_from_account_communities,
    evaluate_account_quantities, is_hub, priority_band, score_v2, token_account_index,
    unit_row_quantities,
)

REQUEST_ID = "2136558c-2326-4430-a46d-0793494e9b49"


def tok(symbol):
    return (symbol * 64)[:64]


def t(account, reservation, event="e1", pay=None, dev=None, addr=None, ip=None):
    data = {"accountId": account, "reservationNo": reservation, "eventId": event}
    for key, value in (("paymentHash", pay), ("deviceIdHash", dev), ("addressHash", addr), ("ipHash", ip)):
        if value is not None:
            data[key] = tok(value)
    return data


def req(tickets):
    return Track1AnalysisRequest.model_validate({"requestId": REQUEST_ID, "tickets": tickets})


def own(index):
    """다른 계정과 겹치지 않는 고유 토큰입니다."""
    return f"{index:04x}"


class AccountAggregationTests(unittest.TestCase):
    def test_same_account_tokens_are_deduplicated(self):
        accounts = aggregate_accounts(req([t("a", "1", pay="a"), t("a", "2", pay="a"), t("a", "3", pay="b")]).tickets)
        account = accounts[("e1", "a")]
        self.assertEqual(account.ticket_count, 3)
        self.assertEqual(len(account.tokens["payment_hash"]), 2)

    def test_single_account_multi_ticket_has_no_edges(self):
        for quantity in (2, 3):
            with self.subTest(quantity=quantity):
                graph = build_account_token_graph(
                    req([t("solo", str(i), pay="a", dev="b") for i in range(quantity)]).tickets
                )
                self.assertEqual(graph.edges, ())
                self.assertEqual(graph.accounts, ())

    def test_two_accounts_sharing_token_create_one_token_two_edges(self):
        graph = build_account_token_graph(req([
            t("a", "1", pay="a"), t("a", "2", pay="a"), t("b", "3", pay="a"),
        ]).tickets)
        self.assertEqual(len(graph.accounts), 2)
        self.assertEqual(len(graph.tokens), 1)
        self.assertEqual(len(graph.edges), 2)
        self.assertEqual(graph.node_count, 3)

    def test_unshared_tokens_and_unconnected_accounts_are_not_projected(self):
        graph = build_account_token_graph(req([
            t("a", "1", pay="a", dev="1"), t("b", "2", pay="a", dev="2"), t("c", "3", pay="c"),
        ]).tickets)
        self.assertEqual([account.key[1] for account in graph.accounts], ["a", "b"])
        self.assertEqual([key[1] for key in graph.tokens], ["payment_hash"])

    def test_signal_types_are_separate_token_nodes(self):
        graph = build_account_token_graph(req([
            t("a", "1", pay="a", ip="a"), t("b", "2", pay="a", ip="a"),
        ]).tickets)
        self.assertEqual({key[1] for key in graph.tokens}, {"payment_hash", "ip_hash"})

    def test_events_are_isolated_for_token_and_account(self):
        graph = build_account_token_graph(req([
            t("a", "1", "e1", pay="a"), t("b", "2", "e2", pay="a"), t("a", "3", "e2", dev="d"),
        ]).tickets)
        self.assertEqual(graph.edges, ())

    def test_input_order_does_not_change_graph(self):
        tickets = [t("a", "1", pay="a"), t("b", "2", pay="a", dev="d"), t("c", "3", dev="d")]
        first = build_account_token_graph(req(tickets).tickets)
        second = build_account_token_graph(req(list(reversed(tickets))).tickets)
        self.assertEqual(first.edges, second.edges)
        self.assertEqual(first.tokens, second.tokens)


class HubTests(unittest.TestCase):
    def _ip_group(self, count, event="e1"):
        return [t(f"a{i}", f"r{i}", event, ip="f", pay=own(i)) for i in range(count)]

    def test_hub_limits_are_strictly_greater(self):
        self.assertFalse(is_hub("ip_hash", 8))
        self.assertTrue(is_hub("ip_hash", 9))
        self.assertFalse(is_hub("address_hash", 6))
        self.assertTrue(is_hub("address_hash", 7))
        self.assertFalse(is_hub("payment_hash", 1000))

    def test_hub_is_removed_before_projection(self):
        graph = build_account_token_graph(req(self._ip_group(9)).tickets)
        self.assertEqual(graph.edges, ())
        self.assertEqual(len(graph.excluded_hubs), 1)

    def test_at_limit_is_projected(self):
        graph = build_account_token_graph(req(self._ip_group(8)).tickets)
        self.assertEqual(len(graph.accounts), 8)
        self.assertEqual(graph.excluded_hubs, ())

    def test_hub_counts_distinct_accounts_not_tickets(self):
        tickets = [t("solo", f"r{i}", ip="f") for i in range(20)] + [t("b", "x", ip="f")]
        self.assertEqual(build_account_token_graph(req(tickets).tickets).excluded_hubs, ())

    def test_hub_is_scoped_per_event(self):
        tickets = self._ip_group(5, "e1") + [
            t(f"z{i}", f"z{i}", "e2", ip="f", pay=own(100 + i)) for i in range(5)
        ]
        self.assertEqual(build_account_token_graph(req(tickets).tickets).excluded_hubs, ())

    def test_hub_count_uses_whole_request_scope(self):
        index = token_account_index(aggregate_accounts(req(self._ip_group(9)).tickets))
        self.assertEqual(len(index[("e1", "ip_hash", tok("f"))]), 9)


class ScoreTests(unittest.TestCase):
    FULL = {"payment_hash": 1.0, "device_id_hash": 1.0, "address_hash": 1.0, "ip_hash": 1.0}
    ZERO = {"payment_hash": 0.0, "device_id_hash": 0.0, "address_hash": 0.0, "ip_hash": 0.0}

    def only(self, **values):
        return {**self.ZERO, **values}

    def test_range_and_full_relation(self):
        self.assertEqual(score_v2(5, self.FULL), 1.0)
        self.assertLessEqual(score_v2(50, self.FULL), 1.0)

    def test_weight_sum_is_normalised(self):
        self.assertAlmostEqual(DEFAULT_PARAMETERS.weight_sum, 1.05)

    def test_requires_two_accounts(self):
        with self.assertRaises(ValueError):
            score_v2(1, self.FULL)

    def test_more_accounts_increase_priority_until_saturation(self):
        coverage = self.only(payment_hash=1.0, device_id_hash=1.0)
        self.assertLess(score_v2(2, coverage), score_v2(5, coverage))
        self.assertEqual(score_v2(5, coverage), score_v2(9, coverage))

    def test_ip_only_is_never_high_priority(self):
        score = score_v2(8, self.only(ip_hash=1.0))
        self.assertLess(score, 0.1)
        self.assertEqual(priority_band(score), "LOW")

    def test_weak_only_stays_low(self):
        self.assertEqual(priority_band(score_v2(8, self.only(address_hash=1.0, ip_hash=1.0))), "LOW")

    def test_single_payment_is_not_top_band(self):
        self.assertNotEqual(priority_band(score_v2(6, self.only(payment_hash=1.0))), "HIGH")

    def test_multi_signal_outranks_single_signal(self):
        self.assertGreater(
            score_v2(4, self.only(payment_hash=1.0, device_id_hash=1.0)),
            score_v2(4, self.only(payment_hash=1.0)),
        )

    def test_score_depends_on_accounts_not_ticket_count(self):
        one_each = [t("a", "1", pay="a", dev="d"), t("b", "2", pay="a", dev="d")]
        many = one_each + [t("a", "3", pay="a", dev="d"), t("b", "4", pay="a", dev="d")]
        self.assertEqual(
            proxy_v2_clusters(req(one_each))[0].risk_score,
            proxy_v2_clusters(req(many))[0].risk_score,
        )

    def test_parameters_are_versioned_override(self):
        strict = ScoreParameters(hub_account_limits={"ip_hash": 1})
        self.assertTrue(is_hub("ip_hash", 2, strict))


class PatternTests(unittest.TestCase):
    """R, N 패턴 라벨은 입력에 없고 구조만으로 계산되는지 확인합니다."""

    def cluster_for(self, tickets):
        clusters = proxy_v2_clusters(req(tickets))
        self.assertEqual(len(clusters), 1)
        return clusters[0]

    def test_r1_dense_relation_scores_high(self):
        cluster = self.cluster_for([
            t(f"a{i}", f"r{i}", pay="a", dev="b", addr="c", ip="d") for i in range(6)
        ])
        self.assertEqual(priority_band(cluster.risk_score), "HIGH")

    def test_r2_indirect_chain_is_one_cluster(self):
        cluster = self.cluster_for([
            t("a0", "r0", pay="1", dev=own(1)),
            t("a1", "r1", pay="1", dev="a"),
            t("a2", "r2", pay="2", dev="a"),
            t("a3", "r3", pay="2", dev="b"),
            t("a4", "r4", pay="3", dev="b"),
            t("a5", "r5", pay="3", dev=own(2)),
        ])
        self.assertEqual(len({m.account_id for m in cluster.members}), 6)
        self.assertEqual(priority_band(cluster.risk_score), "HIGH")

    def test_r3_composite_is_at_least_medium(self):
        cluster = self.cluster_for([
            t("a0", "r0", pay="1", ip="a"),
            t("a1", "r1", pay="1", dev="2"),
            t("a2", "r2", addr="3", dev="2"),
            t("a3", "r3", addr="3", dev="2", ip="b"),
            t("a4", "r4", dev="2", ip="a"),
        ])
        self.assertIn(priority_band(cluster.risk_score), ("MEDIUM", "HIGH"))

    def test_n1_family_stays_low(self):
        cluster = self.cluster_for([t(f"a{i}", f"r{i}", addr="1", ip="2") for i in range(4)])
        self.assertEqual(priority_band(cluster.risk_score), "LOW")

    def test_n2_public_ip_hub_makes_no_cluster(self):
        self.assertEqual(proxy_v2_clusters(req([t(f"a{i}", f"r{i}", ip="9") for i in range(30)])), [])

    def test_n3_group_payment_is_review_not_top(self):
        cluster = self.cluster_for([t(f"a{i}", f"r{i}", pay="3") for i in range(6)])
        self.assertEqual(priority_band(cluster.risk_score), "MEDIUM")


class ClusterMappingTests(unittest.TestCase):
    def test_members_expand_to_real_reservations_only(self):
        request = req([
            t("a", "1", pay="a"), t("a", "2", pay="a"), t("b", "3", pay="a"), t("c", "4", pay="c"),
        ])
        cluster = proxy_v2_clusters(request)[0]
        self.assertEqual({m.reservation_no for m in cluster.members}, {"1", "2", "3"})

    def test_spring_contract_invariants(self):
        tickets = [
            t("a", "1", pay="a", dev="1"), t("a", "2", pay="a", dev="1"), t("b", "3", pay="a"),
            t("c", "4", dev="d", ip="9"), t("d", "5", dev="d"),
        ]
        request = req(tickets)
        submitted = {ticket.key: ticket for ticket in request.tickets}
        seen = set()
        for cluster in proxy_v2_clusters(request):
            self.assertGreaterEqual(len(cluster.members), 2)
            self.assertGreaterEqual(len({m.account_id for m in cluster.members}), 2)
            tokens = {"payment": set(), "device": set(), "address": set(), "ip": set()}
            for member in cluster.members:
                key = (member.account_id, member.reservation_no, member.event_id)
                self.assertIn(key, submitted)
                self.assertNotIn(key, seen)
                seen.add(key)
                tokens["payment"].add(submitted[key].payment_hash)
                tokens["device"].add(submitted[key].device_id_hash)
                tokens["address"].add(submitted[key].address_hash)
                tokens["ip"].add(submitted[key].ip_hash)
            self.assertTrue(set(cluster.payment_hashes) <= tokens["payment"])
            self.assertTrue(set(cluster.device_id_hashes) <= tokens["device"])
            self.assertTrue(set(cluster.address_hashes) <= tokens["address"])
            self.assertTrue(set(cluster.ip_hashes) <= tokens["ip"])
            self.assertTrue(
                cluster.payment_hashes or cluster.device_id_hashes
                or cluster.address_hashes or cluster.ip_hashes
            )

    def test_cluster_evidence_excludes_hub_tokens(self):
        tickets = [t(f"a{i}", f"r{i}", ip="f", pay="a" if i < 2 else own(i)) for i in range(9)]
        cluster = proxy_v2_clusters(req(tickets))[0]
        self.assertEqual(cluster.ip_hashes, [])
        self.assertEqual(cluster.payment_hashes, [tok("a")])

    def test_partition_splitting_token_owners_drops_unbacked_community(self):
        request = req([t("a", "1", pay="a"), t("b", "2", pay="a"), t("c", "3", dev="d"), t("e", "4", dev="d")])
        graph = build_account_token_graph(request.tickets)
        # 공유 토큰 소유 계정을 어긋나게 묶으면 근거가 없는 커뮤니티가 됩니다.
        records = [
            {"accountIndex": 0, "communityId": 1}, {"accountIndex": 2, "communityId": 1},
            {"accountIndex": 1, "communityId": 2}, {"accountIndex": 3, "communityId": 2},
        ]
        self.assertEqual(clusters_from_account_communities(request, graph, records), [])

    def test_incomplete_or_invalid_account_mapping_is_rejected(self):
        request = req([t("a", "1", pay="a"), t("b", "2", pay="a")])
        graph = build_account_token_graph(request.tickets)
        for records in (
            [{"accountIndex": 0, "communityId": 1}],
            [{"accountIndex": 0, "communityId": 1}, {"accountIndex": 0, "communityId": 1}],
            [{"accountIndex": 0, "communityId": 1}, {"accountIndex": 5, "communityId": 1}],
            [{"accountIndex": "0", "communityId": 1}, {"accountIndex": 1, "communityId": 1}],
        ):
            with self.subTest(records=records):
                with self.assertRaises(GraphResultInvalid):
                    clusters_from_account_communities(request, graph, records)


class QuantityRuleTests(unittest.TestCase):
    POLICY = {"e1": PurchaseLimitPolicy(2, "policy-test-1")}

    def evaluate(self, tickets, policies=None, complete=("e1",)):
        quantities = unit_row_quantities(req(tickets).tickets)
        return evaluate_account_quantities(quantities, self.POLICY if policies is None else policies, complete)

    def test_single_account_two_is_within_limit(self):
        result = self.evaluate([t("a", "1", pay="a"), t("a", "2", pay="a")])[0]
        self.assertEqual(result.status, QUANTITY_EVALUATED)
        self.assertEqual(result.findings, ())

    def test_single_account_three_is_separate_signal_and_not_cluster(self):
        tickets = [t("a", str(i), pay="a") for i in range(3)]
        result = self.evaluate(tickets)[0]
        self.assertEqual([(f.account_key[1], f.valid_quantity) for f in result.findings], [("a", 3)])
        self.assertEqual(proxy_v2_clusters(req(tickets)), [])

    def test_two_accounts_with_two_each_do_not_exceed_limit(self):
        result = self.evaluate([t(a, f"{a}{i}", pay="a") for a in "ab" for i in range(2)])[0]
        self.assertEqual(result.findings, ())

    def test_missing_policy_is_not_evaluated(self):
        result = self.evaluate([t("a", str(i), pay="a") for i in range(5)], policies={})[0]
        self.assertEqual((result.status, result.reason), (QUANTITY_NOT_EVALUATED, REASON_POLICY_MISSING))
        self.assertEqual(result.findings, ())

    def test_incomplete_quantity_is_not_evaluated(self):
        result = self.evaluate([t("a", str(i), pay="a") for i in range(5)], complete=())[0]
        self.assertEqual((result.status, result.reason), (QUANTITY_NOT_EVALUATED, REASON_QUANTITY_INCOMPLETE))

    def test_policy_is_per_event(self):
        policies = {"e1": PurchaseLimitPolicy(2, "p1"), "e2": PurchaseLimitPolicy(4, "p2")}
        tickets = [t("a", f"{e}{i}", e, pay="a") for e in ("e1", "e2") for i in range(3)]
        results = {r.event_id: r for r in self.evaluate(tickets, policies, ("e1", "e2"))}
        self.assertEqual(len(results["e1"].findings), 1)
        self.assertEqual(results["e2"].findings, ())

    def test_invalid_policy_and_quantity_are_rejected(self):
        for bad in (0, -1, True, "2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PurchaseLimitPolicy(bad, "p")
        with self.assertRaises(ValueError):
            PurchaseLimitPolicy(2, " ")
        with self.assertRaises(ValueError):
            evaluate_account_quantities({("e1", "a"): -1}, self.POLICY, ("e1",))


if __name__ == "__main__":
    unittest.main()
