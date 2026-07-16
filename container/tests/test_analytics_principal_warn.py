"""
#483: Cost Reports must warn when a CUR query runs but carries no
per-user identity (line_item_iam_principal) — not show a silently
unattributed report.

Unit tests for analytics._principal_data_present (no DB / no
Athena — pure result-shape logic).
"""
from __future__ import annotations

from api.routes.analytics import _principal_data_present as present


def test_column_absent_treated_as_present():
    # Most saved queries are aggregates that don't select the
    # principal column — we can't assess, so don't warn.
    assert present(["email", "cost"], [["a@x", "1.00"]]) is True


def test_principal_column_with_values_is_present():
    assert present(
        ["line_item_iam_principal", "cost"],
        [["arn:aws:sts::1:assumed-role/r/a@x", "1.00"]],
    ) is True


def test_principal_column_all_blank_is_missing():
    # CUR exists but the IAM-principal allocation toggle is off →
    # every value blank → warn.
    assert present(
        ["line_item_iam_principal", "cost"],
        [["", "1.00"], ["   ", "2.00"]],
    ) is False


def test_principal_column_no_rows_does_not_warn():
    # CUR still backfilling — no rows yet. Don't warn (false
    # alarm); the "data not ready" path covers this.
    assert present(["line_item_iam_principal"], []) is True


def test_ragged_row_shorter_than_columns_is_safe():
    # Defensive: a row missing the principal cell must not crash.
    assert present(
        ["a", "line_item_iam_principal"],
        [["only-one-cell"]],
    ) is False
