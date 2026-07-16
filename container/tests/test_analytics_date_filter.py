"""
Date-range across all Cost Reports + featured-first ordering.

Unit tests for the pure analytics helpers (no DB / no Athena):
  - _build_date_filter / _substitute_date_filter — the {{DATE_FILTER}}
    swap and its ISO + ordering injection guard;
  - _cache_key — range-aware cache key (a picked range must not collide
    with the default-MTD entry);
  - _query_sort_key — the new token/cost report pins first.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.routes.analytics import (
    _build_date_filter,
    _substitute_date_filter,
    _cache_key,
    _query_sort_key,
    _DATE_FILTER_DEFAULT,
    _DATE_FILTER_TOKEN,
    FEATURED_QUERIES,
)


# ── _build_date_filter: default + range ──────────────────────────────
def test_no_range_is_month_to_date():
    # Both blank → the historic MTD default predicate (unchanged
    # behaviour for a bare run).
    assert _build_date_filter(None, None) == _DATE_FILTER_DEFAULT
    assert _build_date_filter("", "") == _DATE_FILTER_DEFAULT


def test_valid_range_builds_half_open_window():
    sql = _build_date_filter("2026-05-01", "2026-06-01")
    assert "line_item_usage_start_date >= TIMESTAMP '2026-05-01" in sql
    assert "line_item_usage_start_date < TIMESTAMP '2026-06-01" in sql


# ── _build_date_filter: the injection guard ──────────────────────────
@pytest.mark.parametrize("bad", [
    "2026-5-1",            # not zero-padded
    "26-05-01",            # 2-digit year
    "2026/05/01",          # wrong separator
    "2026-05-01; DROP",    # SQL injection attempt
    "2026-05-01' OR '1",   # quote-break attempt
    "yesterday",
    "  ",
])
def test_non_iso_start_rejected(bad):
    with pytest.raises(HTTPException) as e:
        _build_date_filter(bad, "2026-06-01")
    assert e.value.status_code == 400


@pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01"])
def test_impossible_calendar_date_rejected(bad):
    # Regex-valid shape but not a real date — fromisoformat catches it.
    with pytest.raises(HTTPException) as e:
        _build_date_filter("2026-01-01", bad)
    assert e.value.status_code == 400


def test_end_before_start_rejected():
    with pytest.raises(HTTPException) as e:
        _build_date_filter("2026-06-02", "2026-06-01")
    assert e.value.status_code == 400


def test_equal_start_end_allowed():
    # start == end is a valid (empty half-open) window, not an error.
    sql = _build_date_filter("2026-06-01", "2026-06-01")
    assert "2026-06-01" in sql


def test_half_given_range_rejected():
    # start without end (or vice-versa) is a client bug, not MTD.
    with pytest.raises(HTTPException):
        _build_date_filter("2026-06-01", None)
    with pytest.raises(HTTPException):
        _build_date_filter(None, "2026-06-01")


# ── _substitute_date_filter ──────────────────────────────────────────
def test_substitute_swaps_token():
    sql = f"WHERE x = 1 AND {_DATE_FILTER_TOKEN}\nGROUP BY 1"
    out = _substitute_date_filter(sql, None, None)
    assert _DATE_FILTER_TOKEN not in out
    assert _DATE_FILTER_DEFAULT in out


def test_substitute_is_noop_without_token():
    # A query that doesn't carry the token (e.g. daily-trend) runs
    # verbatim — back-compat.
    sql = "SELECT * WHERE line_item_usage_start_date >= CURRENT_DATE"
    assert _substitute_date_filter(sql, None, None) == sql


def test_substitute_validates_even_without_token():
    # An invalid range is rejected even when the SQL has no token —
    # better a 400 than silently ignoring a bad range.
    with pytest.raises(HTTPException):
        _substitute_date_filter("SELECT 1", "bad", "also-bad")


def test_substitute_applies_range_predicate():
    sql = f"AND {_DATE_FILTER_TOKEN}"
    out = _substitute_date_filter(sql, "2026-05-01", "2026-06-01")
    assert "2026-05-01" in out and "2026-06-01" in out
    assert _DATE_FILTER_TOKEN not in out


# ── _cache_key: range awareness (correctness) ────────────────────────
def test_cache_key_default_is_bare_query_id():
    # Default (no range) keeps the bare id so historic MTD cache
    # entries still hit.
    assert _cache_key("q1", None, None) == "q1"
    assert _cache_key("q1", "", "") == "q1"


def test_cache_key_includes_range():
    k = _cache_key("q1", "2026-05-01", "2026-06-01")
    assert k != "q1"
    assert "2026-05-01" in k and "2026-06-01" in k


def test_different_ranges_have_different_keys():
    # The bug a query_id-only key would cause: a cached May result
    # served for a June request.
    may = _cache_key("q1", "2026-05-01", "2026-06-01")
    jun = _cache_key("q1", "2026-06-01", "2026-07-01")
    assert may != jun


# ── _query_sort_key: featured-first ──────────────────────────────────
def test_featured_query_sorts_first():
    featured = FEATURED_QUERIES[0]
    queries = [
        {"name": "tg-bedrock-spend-by-user", "group": "cur"},
        {"name": featured, "group": "usage"},
        {"name": "tg-bedrock-daily-trend", "group": "cur"},
    ]
    queries.sort(key=_query_sort_key)
    # The featured report is first even though its group is 'usage'
    # (alphabetically/group-wise it would NOT lead).
    assert queries[0]["name"] == featured


def test_non_featured_keep_group_then_name_order():
    queries = [
        {"name": "tg-bedrock-spend-by-user", "group": "usage"},
        {"name": "tg-bedrock-daily-trend", "group": "cur"},
    ]
    queries.sort(key=_query_sort_key)
    # cur before usage for non-featured.
    assert queries[0]["group"] == "cur"


# ── #1122: residual {{…}} placeholder guard ──────────────────────────

def test_known_date_filter_token_still_substitutes():
    # No regression: {{DATE_FILTER}} resolves and the residual scan finds
    # nothing left over.
    sql = "SELECT 1 WHERE " + _DATE_FILTER_TOKEN
    out = _substitute_date_filter(sql, "2026-06-01", "2026-06-02")
    assert _DATE_FILTER_TOKEN not in out
    assert "line_item_usage_start_date" in out
    assert "{{" not in out


def test_tokenless_query_runs_verbatim():
    # A query with no placeholder at all → returned unchanged, no false
    # positive from the guard.
    sql = "SELECT count(*) FROM data"
    assert _substitute_date_filter(sql, None, None) == sql


def test_unknown_placeholder_fails_loud_naming_token():
    # An unknown {{…}} after substitution → 500 naming the token + the
    # image-too-old cause, NOT a pass-through to Athena.
    sql = "SELECT 1 WHERE x = 1 AND {{NOT_A_REAL_TOKEN}}"
    with pytest.raises(HTTPException) as e:
        _substitute_date_filter(sql, None, None)
    assert e.value.status_code == 500
    assert "{{NOT_A_REAL_TOKEN}}" in e.value.detail
    assert "older than the report" in e.value.detail


def test_unknown_placeholder_alongside_known_token():
    # {{DATE_FILTER}} resolves but a second unknown token remains → still
    # fails loud (the residual scan runs AFTER the known swap).
    sql = ("SELECT 1 WHERE " + _DATE_FILTER_TOKEN
           + " AND col = {{MYSTERY}}")
    with pytest.raises(HTTPException) as e:
        _substitute_date_filter(sql, "2026-06-01", "2026-06-02")
    assert e.value.status_code == 500
    assert "{{MYSTERY}}" in e.value.detail


def test_multiple_unknown_placeholders_all_named():
    sql = "SELECT {{A}} , {{B}} FROM data"
    with pytest.raises(HTTPException) as e:
        _substitute_date_filter(sql, None, None)
    assert "{{A}}" in e.value.detail and "{{B}}" in e.value.detail


def test_unknown_placeholder_on_tokenless_path():
    # The version-skew case: the query never used {{DATE_FILTER}} but
    # carries a different new token the old image doesn't know — the
    # guard must fire on this branch too (not just the swap branch).
    sql = "SELECT 1 WHERE {{NEW_IN_A_LATER_TEMPLATE}}"
    with pytest.raises(HTTPException) as e:
        _substitute_date_filter(sql, None, None)
    assert e.value.status_code == 500
    assert "{{NEW_IN_A_LATER_TEMPLATE}}" in e.value.detail


def test_whitespace_in_placeholder_is_caught():
    # The regex tolerates inner whitespace ({{ X }}) so a sloppily-typed
    # token is still caught, not passed through.
    sql = "SELECT 1 WHERE {{ SPACED }}"
    with pytest.raises(HTTPException) as e:
        _substitute_date_filter(sql, None, None)
    assert "{{SPACED}}" in e.value.detail
