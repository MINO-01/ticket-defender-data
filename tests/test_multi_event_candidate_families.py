from services.multi_event_candidate_families import assign_candidate_families, build_evidence_reasons


def test_candidate_families_use_smallest_strict_superset_and_ignore_partial_overlap():
    account_sets = {
        "candidate-child": {"a", "b"},
        "candidate-parent-large": {"a", "b", "c", "d"},
        "candidate-parent-small": {"a", "b", "c"},
        "candidate-partial": {"b", "c", "x"},
    }
    scores = {
        "candidate-child": 0.62,
        "candidate-parent-large": 0.31,
        "candidate-parent-small": 0.28,
        "candidate-partial": 0.62,
    }

    result = assign_candidate_families(account_sets, scores)

    assert result["candidate-child"] == {
        "parent_candidate_id": "candidate-parent-small",
        "family_id": "candidate-parent-large",
        "child_count": 0,
        "family_score": 0.62,
    }
    assert result["candidate-parent-small"]["parent_candidate_id"] == "candidate-parent-large"
    assert result["candidate-parent-small"]["child_count"] == 1
    assert result["candidate-partial"]["family_id"] == "candidate-partial"
    assert result["candidate-partial"]["parent_candidate_id"] is None


def test_equal_size_parent_ties_use_candidate_id_and_family_score_is_maximum():
    account_sets = {
        "child": {"a"},
        "parent-z": {"a", "z"},
        "parent-a": {"a", "b"},
    }

    result = assign_candidate_families(account_sets, {"child": 0.4, "parent-z": 0.2, "parent-a": 0.3})

    assert result["child"]["parent_candidate_id"] == "parent-a"
    assert result["child"]["family_score"] == 0.4
    assert result["parent-z"]["family_id"] == "parent-z"


def test_evidence_counts_distinct_members_and_member_peers_without_returning_tokens():
    reason_rows, member_rows = build_evidence_reasons(
        {"account-a", "account-b", "account-c"},
        {"performance-1", "performance-2"},
        [
            ("refund_account_hash", "performance-1", {"account-a", "account-b"}),
            ("refund_account_hash", "performance-2", {"account-a", "account-b", "account-c"}),
        ],
    )

    refund = next(item for item in reason_rows if item["code"] == "SHARED_REFUND_ACCOUNT")
    assert refund == {
        "code": "SHARED_REFUND_ACCOUNT",
        "count": 3,
        "performances": ["performance-1", "performance-2"],
        "strengthRank": 1,
    }
    repeated = next(item for item in reason_rows if item["code"] == "REPEATED_PERFORMANCES")
    assert repeated == {
        "code": "REPEATED_PERFORMANCES",
        "count": 2,
        "performances": ["performance-1", "performance-2"],
        "strengthRank": 2,
    }
    assert len(member_rows) == 3
    account_c = next(item for item in member_rows if item["accountReferenceHmac"] == "account-c")
    assert account_c["reasons"][0]["peerCount"] == 2
    assert "token" not in str(reason_rows).lower()
    assert "accountReferenceHmac" in member_rows[0]


def test_member_recurrence_uses_only_that_members_observed_candidate_performances():
    candidate_reasons, member_rows = build_evidence_reasons(
        {"account-a", "account-b"},
        {"performance-1", "performance-2"},
        [],
        {"account-a": {"performance-1", "performance-2"}, "account-b": {"performance-2"}},
    )

    assert candidate_reasons == [{
        "code": "REPEATED_PERFORMANCES", "count": 1,
        "performances": ["performance-1", "performance-2"], "strengthRank": 1,
    }]

    account_a = next(item for item in member_rows if item["accountReferenceHmac"] == "account-a")
    account_b = next(item for item in member_rows if item["accountReferenceHmac"] == "account-b")
    assert account_a["reasons"] == [{
        "code": "REPEATED_PERFORMANCES", "peerCount": 0,
        "performances": ["performance-1", "performance-2"],
    }]
    assert account_b["reasons"] == []


def test_candidate_repeat_reason_is_omitted_when_no_member_repeats():
    candidate_reasons, member_rows = build_evidence_reasons(
        {"account-a", "account-b"},
        {"performance-1", "performance-2"},
        [],
        {"account-a": {"performance-1"}, "account-b": {"performance-2"}},
    )

    assert candidate_reasons == []
    assert all(item["reasons"] == [] for item in member_rows)
