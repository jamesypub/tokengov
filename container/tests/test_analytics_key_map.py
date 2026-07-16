"""
Layer-2: Cost Reports + Activity run Athena directly and GROUP BY
line_item_iam_principal, so a mapped Bedrock-key IAM-user would show as
the opaque `user/<name>`. analytics._apply_key_map rewrites a mapped
key-user cell to the owning developer's email at DISPLAY time.

Unit tests for the pure rewrite helper (no DB / no Athena — the
key_map is passed in, mirroring the two call sites in run_query).
"""
from __future__ import annotations

from api.routes.analytics import _apply_key_map

_COLS = ["line_item_iam_principal", "spend_usd"]
_MAP = {"MantleApiKey-uhbhn79a": "dev@corp.com"}


def _rows(*principals):
    return [[p, "1.23"] for p in principals]


def test_maps_user_slash_name_to_email():
    rows = _rows("user/MantleApiKey-uhbhn79a")
    _apply_key_map(_COLS, rows, _MAP)
    assert rows[0][0] == "dev@corp.com"


def test_maps_full_arn_form_to_email():
    rows = _rows("arn:aws:iam::123:user/MantleApiKey-uhbhn79a")
    _apply_key_map(_COLS, rows, _MAP)
    assert rows[0][0] == "dev@corp.com"


def test_maps_bare_name_form_to_email():
    # Some queries surface the already-projected bare name.
    rows = _rows("MantleApiKey-uhbhn79a")
    _apply_key_map(_COLS, rows, _MAP)
    assert rows[0][0] == "dev@corp.com"


def test_unmapped_key_user_untouched():
    # Regression: a key with no mapping keeps its raw principal.
    rows = _rows("user/BedrockAPIKey-toqd")
    _apply_key_map(_COLS, rows, _MAP)
    assert rows[0][0] == "user/BedrockAPIKey-toqd"


def test_non_key_principals_untouched():
    # THE matching rule: the map is the discriminator — a service role,
    # an AWSReservedSSO_* session, and a human email are NEVER rewritten.
    raw = [
        "arn:aws:sts::123:assumed-role/MyEcsTaskRole/ecs-abc",
        "arn:aws:sts::123:assumed-role/AWSReservedSSO_Dev_x/bob@corp.com",
        "alice@corp.com",
    ]
    rows = _rows(*raw)
    _apply_key_map(_COLS, rows, _MAP)
    assert [r[0] for r in rows] == raw


def test_empty_map_is_noop():
    rows = _rows("user/MantleApiKey-uhbhn79a")
    _apply_key_map(_COLS, rows, {})
    assert rows[0][0] == "user/MantleApiKey-uhbhn79a"


def test_no_principal_column_is_noop():
    # An aggregate query that doesn't select the principal column.
    cols = ["model_id", "spend_usd"]
    rows = [["claude", "9.99"]]
    _apply_key_map(cols, rows, _MAP)
    assert rows == [["claude", "9.99"]]
