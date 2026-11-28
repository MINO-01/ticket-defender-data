from datetime import datetime, timedelta, timezone

import pytest

from services.track1_scoring_exp2 import quantity_context
from services.track1_scoring_exp3 import (
    E1Event,
    E2Candidate,
    E1Relation,
    PerformanceScope,
    build_e1_matches,
    build_e1_relations,
    e2_hub_excluded,
    e1_coverage,
    e3_ip_window_tokens,
    normalize_seat_component,
    priority_band_exp3,
    qualified_e2_neighbors,
    recurrence_r3,
    score_exp3,
    shared_e2_resource_types,
    E2ResourceObservation,
)


UTC = timezone.utc


def event(
    account: str,
    ticket: str,
    event_type: str,
    at: datetime | None,
    *,
    seat: tuple[str, str, str] = ("A", "01", "001"),
    tenant: str = "tenant-1",
    source: str = "supplier-1",
    performance: str = "performance-1",
) -> E1Event:
    event_time = {"reserved_at": at} if event_type == "RESERVATION" else {"cancelled_at": at}
    return E1Event(
        tenant_id=tenant,
        source_system=source,
        performance_id=performance,
        account_hmac=account,
        ticket_hmac=ticket,
        event_type=event_type,
        **event_time,
        seat_section=seat[0],
        seat_row=seat[1],
        seat_number=seat[2],
    )


def test_seat_normalization_preserves_case_and_leading_zeroes():
    assert normalize_seat_component("  Ａ  ") == "A"
    assert normalize_seat_component(" a ") == "a"
    assert normalize_seat_component(" ００１ ") == "001"
    assert normalize_seat_component("  ") is None


def test_e1_uses_one_recent_unused_cancellation_per_reservation_and_tie_breaks():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = [
        event("account-a", "ticket-z", "CANCELLATION", start),
        event("account-c", "ticket-a", "CANCELLATION", start),
        event("account-b", "reserve-1", "RESERVATION", start + timedelta(seconds=60)),
        event("account-d", "reserve-2", "RESERVATION", start + timedelta(seconds=70)),
    ]

    matches = build_e1_matches(rows)

    assert len(matches) == 2
    assert [match.cancelled_ticket_hmac for match in matches] == ["ticket-a", "ticket-z"]
    assert matches[0].cancelled_by_account == "account-c"
    assert matches[0].reserved_by_account == "account-b"


def test_e1_window_is_inclusive_at_90_seconds_and_rejects_91_seconds():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = [
        event("account-a", "cancel-90", "CANCELLATION", start),
        event("account-b", "reserve-90", "RESERVATION", start + timedelta(seconds=90)),
        event("account-c", "cancel-91", "CANCELLATION", start),
        event("account-d", "reserve-91", "RESERVATION", start + timedelta(seconds=91), seat=("A", "01", "002")),
    ]

    matches = build_e1_matches(rows)

    assert [(match.cancelled_ticket_hmac, match.reserved_ticket_hmac) for match in matches] == [
        ("cancel-90", "reserve-90"),
    ]


def test_e1_missing_reserved_at_does_not_fall_back_to_another_time():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = [
        event("account-a", "cancel", "CANCELLATION", start),
        event("account-b", "reservation-without-reserved-at", "RESERVATION", None),
    ]

    assert build_e1_matches(rows) == ()


def test_e1_pairs_follow_cancel_rebook_chain_and_never_cross_scope():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = [
        event("account-a", "cancel-a", "CANCELLATION", start),
        event("account-b", "reserve-b", "RESERVATION", start + timedelta(seconds=30)),
        event("account-b", "cancel-b", "CANCELLATION", start + timedelta(seconds=40)),
        event("account-c", "reserve-c", "RESERVATION", start + timedelta(seconds=60)),
        event("account-x", "cancel-x", "CANCELLATION", start, source="supplier-2"),
        event("account-y", "reserve-y", "RESERVATION", start + timedelta(seconds=30), source="supplier-3"),
        event("account-m", "cancel-m", "CANCELLATION", start, tenant="tenant-2"),
        event("account-n", "reserve-n", "RESERVATION", start + timedelta(seconds=30)),
        event("account-u", "cancel-u", "CANCELLATION", start, performance="performance-2"),
        event("account-v", "reserve-v", "RESERVATION", start + timedelta(seconds=30)),
    ]

    matches = build_e1_matches(rows)

    assert [(match.cancelled_by_account, match.reserved_by_account) for match in matches] == [
        ("account-a", "account-b"),
        ("account-b", "account-c"),
    ]


def test_e1_duplicate_rows_are_removed_and_conflicting_identity_fails_closed():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    cancel = event("account-a", "cancel-1", "CANCELLATION", start)
    reservation = event("account-b", "reserve-1", "RESERVATION", start + timedelta(seconds=10))

    assert len(build_e1_matches([cancel, cancel, reservation])) == 1
    with pytest.raises(ValueError, match="conflicting E1 duplicate row"):
        build_e1_matches([cancel, event("account-c", "cancel-1", "CANCELLATION", start), reservation])


def test_e1_repeated_class_uses_distinct_seat_keys_not_event_count():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = [
        event("account-a", "c1", "CANCELLATION", start),
        event("account-b", "r1", "RESERVATION", start + timedelta(seconds=10)),
        event("account-a", "c2", "CANCELLATION", start + timedelta(seconds=20)),
        event("account-b", "r2", "RESERVATION", start + timedelta(seconds=30)),
        event("account-a", "c3", "CANCELLATION", start + timedelta(seconds=40), seat=("A", "01", "002")),
        event("account-b", "r3", "RESERVATION", start + timedelta(seconds=50), seat=("A", "01", "002")),
    ]

    relations = build_e1_relations(rows)

    assert len(relations) == 1
    assert relations[0].distinct_seat_count == 2
    assert relations[0].repeated is True


def test_e1_coverage_deduplicates_accounts_and_prioritizes_repeat_type():
    relations = [
        E1Relation("tenant-1", "supplier-1", "p1", "a", "b", 1, False),
        E1Relation("tenant-1", "supplier-1", "p1", "b", "c", 2, True),
        E1Relation("tenant-2", "supplier-1", "p1", "a", "d", 2, True),
    ]

    coverage = e1_coverage(
        {"a", "b", "c", "d"}, relations, tenant_id="tenant-1", source_system="supplier-1",
    )

    assert coverage.single_account_count == 1
    assert coverage.repeated_account_count == 2
    assert coverage.c1 == pytest.approx(0.25)
    assert coverage.c2 == pytest.approx(0.50)
    assert coverage.link == pytest.approx(0.39875)


def candidate(
    candidate_id: str,
    *,
    q: float | None,
    scopes: tuple[PerformanceScope, ...] = (),
    tenant: str = "tenant-1",
    source: str = "supplier-1",
) -> E2Candidate:
    return E2Candidate(candidate_id, "run-1", tenant, source, scopes, q)


def test_e2_requires_an_actual_shared_resource_even_when_q_is_high():
    items = [candidate("a", q=1.0), candidate("b", q=1.0)]

    neighbors = qualified_e2_neighbors(items, {})

    assert neighbors == {"a": frozenset(), "b": frozenset()}


def test_e2_one_resource_is_endpoint_specific_and_does_not_transitively_join():
    items = [candidate("a", q=0.95), candidate("b", q=0.50), candidate("c", q=1.0)]
    shared = {
        ("a", "b"): {"phone_hash"},
        ("b", "c"): {"device_id_hash"},
    }

    neighbors = qualified_e2_neighbors(items, shared)

    assert neighbors["a"] == frozenset({"b"})
    assert neighbors["b"] == frozenset()
    assert neighbors["c"] == frozenset({"b"})
    assert "c" not in neighbors["a"]


def test_e2_two_resource_types_connect_both_direct_endpoints():
    items = [candidate("a", q=None), candidate("b", q=None)]

    neighbors = qualified_e2_neighbors(items, {
        ("a", "b"): {"phone_hash", "refund_account_hash"},
    })

    assert neighbors == {"a": frozenset({"b"}), "b": frozenset({"a"})}


def test_e2_rejects_cross_tenant_candidate_pairs():
    items = [candidate("a", q=1.0), candidate("b", q=1.0, tenant="tenant-2")]

    with pytest.raises(ValueError, match="crosses analysis run, tenant, or source system"):
        qualified_e2_neighbors(items, {("a", "b"): {"phone_hash"}})


def test_e2_resource_discovery_uses_actual_shared_values_and_active_scopes():
    scope_a = PerformanceScope("tenant-1", "supplier-1", "event-a", "performance-a")
    scope_b = PerformanceScope("tenant-1", "supplier-1", "event-b", "performance-b")
    items = [
        candidate("a", q=None, scopes=(scope_a,)),
        candidate("b", q=None, scopes=(scope_b,)),
    ]
    rows = [
        E2ResourceObservation(scope_a, "account-a", "phone_hash", "shared-phone", True, True, True),
        E2ResourceObservation(scope_b, "account-b", "phone_hash", "shared-phone", True, True, True),
        E2ResourceObservation(scope_a, "account-a", "address_hash", "shared-address", True, True, True),
        E2ResourceObservation(scope_b, "account-b", "address_hash", "shared-address", True, True, True),
        E2ResourceObservation(scope_a, "account-a", "refund_account_hash", "cancel-only", False, True, True),
        E2ResourceObservation(scope_b, "account-b", "refund_account_hash", "unverified-period", True, False, True),
    ]

    shared = shared_e2_resource_types(
        items,
        {"a": {"account-a"}, "b": {"account-b"}},
        rows,
        {},
    )

    assert shared == {("a", "b"): frozenset({"phone_hash", "address_hash"})}


def test_e2_applies_existing_device_hub_cutoff_and_no_new_phone_cutoff():
    scope_a = PerformanceScope("tenant-1", "supplier-1", "event-a", "performance-a")
    scope_b = PerformanceScope("tenant-1", "supplier-1", "event-b", "performance-b")
    items = [
        candidate("a", q=None, scopes=(scope_a,)),
        candidate("b", q=None, scopes=(scope_b,)),
    ]
    rows = []
    for index in range(30):
        account = f"account-{index}"
        rows.append(E2ResourceObservation(scope_a, account, "device_id_hash", "shared-device", True, True, True))
        rows.append(E2ResourceObservation(scope_a, account, "phone_hash", "shared-phone", True, True, True))
    rows.extend([
        E2ResourceObservation(scope_b, "account-b", "device_id_hash", "shared-device", True, True, True),
        E2ResourceObservation(scope_b, "account-b", "phone_hash", "shared-phone", True, True, True),
    ])

    shared = shared_e2_resource_types(
        items,
        {"a": {"account-0"}, "b": {"account-b"}},
        rows,
        {scope_a: 30, scope_b: 1},
    )

    assert shared == {("a", "b"): frozenset({"phone_hash"})}
    assert e2_hub_excluded("device_id_hash", 29, 100) is False
    assert e2_hub_excluded("device_id_hash", 30, 100) is True
    assert e2_hub_excluded("address_hash", 50, 1_000) is True
    assert e2_hub_excluded("phone_hash", 10_000, 10_000) is False


def test_r3_counts_distinct_active_scopes_and_only_qualified_direct_neighbors():
    scope_a = PerformanceScope("tenant-1", "supplier-1", "event-a", "performance-a")
    scope_b = PerformanceScope("tenant-1", "supplier-1", "event-b", "performance-b")
    scope_c = PerformanceScope("tenant-1", "supplier-1", "event-c", "performance-c")
    items = [
        candidate("a", q=0.95, scopes=(scope_a, scope_a)),
        candidate("b", q=0.50, scopes=(scope_b, scope_c)),
        candidate("c", q=1.0),
    ]
    direct = qualified_e2_neighbors(items, {("a", "b"): {"phone_hash"}})

    assert recurrence_r3("a", items, direct) == pytest.approx(2 / 3)
    assert recurrence_r3("b", items, direct) == pytest.approx(1 / 3)
    assert recurrence_r3("c", items, direct) == 0.0


def test_e3_uses_reserved_at_and_the_shifted_window_catches_boundary_pair():
    left_at = datetime(2026, 1, 1, 12, 9, 59, tzinfo=UTC)
    right_at = datetime(2026, 1, 1, 12, 10, 1, tzinfo=UTC)
    left = dict(e3_ip_window_tokens("scope", "opaque-ip", left_at))
    right = dict(e3_ip_window_tokens("scope", "opaque-ip", right_at))

    assert left[0] != right[0]
    assert left[300] == right[300]
    assert e3_ip_window_tokens("scope", "opaque-ip", None) == ()
    assert e3_ip_window_tokens("scope", "opaque-ip", datetime(2026, 1, 1, 12, 10)) == ()


def test_exp3_hand_calculation_keeps_base_link_and_applies_coverage_formula():
    accounts = {f"account-{index}" for index in range(12)}
    relations = [
        E1Relation("tenant-1", "supplier-1", "p1", f"account-{index}", f"account-{index + 1}", 1, False)
        for index in range(0, 12, 2)
    ]
    coverage = e1_coverage(accounts, relations, tenant_id="tenant-1", source_system="supplier-1")
    result = score_exp3(12, 0.35, coverage, quantity_context(1.0), 1.0)

    assert coverage.link == pytest.approx(0.30)
    assert result.link == pytest.approx(0.545)
    assert result.score == 0.4833
    assert priority_band_exp3(result.score) == "HIGH"


def test_exp3_with_no_new_signal_preserves_base_link():
    coverage = e1_coverage(
        {"account-a", "account-b"}, (), tenant_id="tenant-1", source_system="supplier-1",
    )

    result = score_exp3(2, 0.42, coverage, quantity_context(0.5), 0.0)

    assert result.link == pytest.approx(0.42)
    assert result.quantity == pytest.approx(0.5)
    assert result.behavior == pytest.approx(0.25)
