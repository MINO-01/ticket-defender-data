from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

import pytest

from services.track1_scoring_exp2 import (
    DEFAULT_EXP2_PARAMETERS,
    RecurrenceCluster,
    build_account_token_graph_exp2,
    damping,
    ip_window_token,
    quantity_context,
    recurrence_scope_counts,
    recurrence_components,
    score_exp2,
)


@dataclass(frozen=True)
class Ticket:
    account_id: str
    reservation_no: str
    scope: str
    purchased_at: datetime | None = None
    payment_hash: str | None = None
    address_hash: str | None = None
    device_id_hash: str | None = None
    ip_hash: str | None = None
    ip_network_type: str | None = None
    refund_account_hash: str | None = None
    phone_hash: str | None = None

    @property
    def analysis_scope(self):
        return self.scope


@pytest.mark.parametrize(("name", "count", "coverage", "damping_values", "q", "p_count", "expected"), [
    ("refund-five", 20, {"refund_account_hash": 1.0}, {"refund_account_hash": 1.0}, 1.0, 5, 0.757),
    ("sandbox-three", 5, {"device_id_hash": 1.0, "ip_window:RESIDENTIAL": 1.0, "payment_hash": 1.0}, {"device_id_hash": 1.0, "ip_window:RESIDENTIAL": 1.0, "payment_hash": math.sqrt(3 / 5)}, 1.0, 3, 0.544),
    ("office-five", 22, {"ip_window:CORPORATE_EDU": 1.0}, {"ip_window:CORPORATE_EDU": 1.0}, 1.0, 5, 0.478),
    ("refund-one", 20, {"refund_account_hash": 1.0}, {"refund_account_hash": 1.0}, 1.0, 1, 0.378),
    ("pcbang-five", 3, {"ip_window:CORPORATE_EDU": 1.0}, {"ip_window:CORPORATE_EDU": 1.0}, 1.0, 5, 0.363),
    ("family-two", 3, {"device_id_hash": 1.0, "ip_window:RESIDENTIAL": 1.0, "payment_hash": 1.0}, {"device_id_hash": 1.0, "ip_window:RESIDENTIAL": 1.0, "payment_hash": 1.0}, 0.5, 2, 0.254),
    ("office-one", 22, {"ip_window:CORPORATE_EDU": 1.0}, {"ip_window:CORPORATE_EDU": 1.0}, 1.0, 1, 0.239),
    ("pcbang-one", 3, {"ip_window:CORPORATE_EDU": 1.0}, {"ip_window:CORPORATE_EDU": 1.0}, 1.0, 1, 0.182),
    ("family-one", 3, {"device_id_hash": 1.0, "ip_window:RESIDENTIAL": 1.0, "payment_hash": 1.0}, {"device_id_hash": 1.0, "ip_window:RESIDENTIAL": 1.0, "payment_hash": 1.0}, 0.5, 1, 0.153),
    ("dormitory", 40, {"ip_window:CORPORATE_EDU": 1.0}, {"ip_window:CORPORATE_EDU": math.sqrt(30 / 40)}, 0.5, 1, 0.108),
    ("mobile", 5, {"ip_window:MOBILE_CGNAT": 1.0}, {"ip_window:MOBILE_CGNAT": math.sqrt(3 / 5)}, 0.5, 1, 0.015),
])
def test_preregistered_a8_scores(name, count, coverage, damping_values, q, p_count, expected):
    result = score_exp2(count, coverage, damping_values, quantity_context(q), p_count)
    assert result.score == pytest.approx(expected, abs=0.001), name


def test_damping_has_soft_hard_and_relative_boundaries():
    assert damping("device_id_hash", 5, 1_000) == 1.0
    assert damping("device_id_hash", 6, 1_000) == pytest.approx(math.sqrt(5 / 6))
    assert damping("device_id_hash", 30, 1_000) is None
    assert damping("payment_hash", 20, 1_000) is None
    assert damping("refund_account_hash", 100, 1_000) == pytest.approx(math.sqrt(10 / 100))


def test_ip_window_is_scope_and_boundary_sensitive():
    base = datetime(2026, 1, 1, 10, 9, 59, tzinfo=timezone.utc)
    a = Ticket("a", "r1", "performance-a", base, ip_hash="a" * 64)
    same = Ticket("b", "r2", "performance-a", base - timedelta(seconds=1), ip_hash="a" * 64)
    next_bucket = Ticket("c", "r3", "performance-a", base + timedelta(seconds=1), ip_hash="a" * 64)
    other_scope = Ticket("d", "r4", "performance-b", base, ip_hash="a" * 64)
    assert ip_window_token(a) == ip_window_token(same)
    assert ip_window_token(a) != ip_window_token(next_bucket)
    assert ip_window_token(a) != ip_window_token(other_scope)
    assert ip_window_token(Ticket("x", "r", "p", None, ip_hash="a" * 64)) is None


def test_graph_is_order_independent_and_excludes_fixed_ip_only_connections():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        Ticket("a", "r1", "p", base, ip_hash="a" * 64, ip_network_type="RESIDENTIAL"),
        Ticket("b", "r2", "p", base + timedelta(seconds=1), ip_hash="a" * 64, ip_network_type="RESIDENTIAL"),
        Ticket("c", "r3", "p", base, ip_hash="b" * 64),
    ]
    first = build_account_token_graph_exp2(rows)
    second = build_account_token_graph_exp2(list(reversed(rows)))
    assert first == second
    assert len(first.edges) == 2
    assert all("ip_hash" not in token for _, token, _ in first.tokens)


def test_mixed_network_type_uses_unknown_signal():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    graph = build_account_token_graph_exp2([
        Ticket("a", "r1", "p", base, ip_hash="a" * 64, ip_network_type="RESIDENTIAL"),
        Ticket("b", "r2", "p", base, ip_hash="a" * 64, ip_network_type="MOBILE_CGNAT"),
    ])
    assert {signal for _, signal, _ in graph.tokens} == {"ip_window:UNKNOWN"}


def test_quantity_neutral_and_invalid_recurrence_are_explicit():
    neutral = quantity_context(None, "POLICY_NOT_CONFIRMED")
    assert neutral.value == 0.5
    assert neutral.status == "NEUTRAL_NO_POLICY"
    with pytest.raises(ValueError):
        score_exp2(2, {"payment_hash": 1.0}, {"payment_hash": 1.0}, p_count=0)


def test_recurrence_joins_only_stable_or_strong_evidence():
    clusters = [
        RecurrenceCluster("a", "p1", frozenset({"stable"}), {"ip_window:RESIDENTIAL": frozenset({"ip"})}),
        RecurrenceCluster("b", "p2", frozenset({"stable"}), {}),
        RecurrenceCluster("c", "p3", frozenset({"other"}), {"ip_window:RESIDENTIAL": frozenset({"ip"})}),
    ]
    counts = recurrence_scope_counts(clusters)
    assert counts == {"a": 2, "b": 2, "c": 1}
    components = recurrence_components(clusters)
    assert {group.cluster_ids: group.scopes for group in components} == {
        ("a", "b"): frozenset({"p1", "p2"}),
        ("c",): frozenset({"p3"}),
    }
