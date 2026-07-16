"""
#749: cur_health distinguishes 'column absent from the delivered
Parquet' (manifest lacks it → re-create the export) from
'present-but-empty' (wait for the next CUR delivery).

Unit tests for the pure S3-path-derivation + manifest-parse helpers
(no DB / no AWS — the path math and JSON shape only). The route-level
state discrimination is covered in test_api_smoke.py with the probe +
manifest stubbed.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from api.routes import cur


# ── _metadata_prefix: swap the trailing data/ for metadata/ ──────────

def test_metadata_prefix_from_standard_location():
    loc = "s3://tg-cur-123-us-east-1/tg-bedrock-cur/tg-bedrock-cur/data/"
    assert cur._metadata_prefix(loc) == (
        "tg-cur-123-us-east-1",
        "tg-bedrock-cur/tg-bedrock-cur/metadata",
    )


def test_metadata_prefix_tolerates_missing_trailing_slash():
    loc = "s3://b/exp/exp/data"
    assert cur._metadata_prefix(loc) == ("b", "exp/exp/metadata")


def test_metadata_prefix_rejects_non_s3_uri():
    assert cur._metadata_prefix("https://example.com/x/data/") is None


def test_metadata_prefix_rejects_non_data_location():
    # A location that isn't the …/data/ leaf shouldn't be rewritten.
    assert cur._metadata_prefix("s3://b/exp/exp/other/") is None


# ── _latest_manifest_key: newest billing period wins ─────────────────

def _s3_list_stub(keys):
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {
        "Contents": [{"Key": k} for k in keys],
        "IsTruncated": False,
    }
    return s3


def test_latest_manifest_picks_newest_billing_period():
    s3 = _s3_list_stub([
        "exp/exp/metadata/BILLING_PERIOD=2025-11/exp-Manifest.json",
        "exp/exp/metadata/BILLING_PERIOD=2026-01/exp-Manifest.json",
        "exp/exp/metadata/BILLING_PERIOD=2025-12/exp-Manifest.json",
        "exp/exp/metadata/BILLING_PERIOD=2026-01/some-other.json",
    ])
    key = cur._latest_manifest_key(s3, "b", "exp/exp/metadata")
    assert key == (
        "exp/exp/metadata/BILLING_PERIOD=2026-01/exp-Manifest.json")


def test_latest_manifest_none_when_no_manifest():
    s3 = _s3_list_stub([
        "exp/exp/metadata/BILLING_PERIOD=2026-01/notes.txt",
    ])
    assert cur._latest_manifest_key(s3, "b", "exp/exp/metadata") is None


# ── _manifest_columns: end-to-end parse with stubbed AWS ─────────────

def _patch_session(monkeypatch, *, location, manifest_doc, keys):
    """Wire get_aws_session().client(...) to return glue + s3 stubs."""
    glue = MagicMock()
    glue.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Location": location}}}
    s3 = _s3_list_stub(keys)
    body = MagicMock()
    body.read.return_value = json.dumps(manifest_doc).encode()
    s3.get_object.return_value = {"Body": body}

    def _client(name):
        return {"glue": glue, "s3": s3}[name]

    sess = MagicMock()
    sess.client.side_effect = _client
    monkeypatch.setattr(cur, "get_aws_session", lambda: sess)


def test_manifest_columns_present(monkeypatch):
    _patch_session(
        monkeypatch,
        location="s3://b/exp/exp/data/",
        manifest_doc={"columns": [
            {"name": "line_item_iam_principal"},
            {"name": "line_item_unblended_cost"},
        ]},
        keys=["exp/exp/metadata/BILLING_PERIOD=2026-01/exp-Manifest.json"],
    )
    cols = cur._manifest_columns()
    assert "line_item_iam_principal" in cols


def test_manifest_columns_absent(monkeypatch):
    _patch_session(
        monkeypatch,
        location="s3://b/exp/exp/data/",
        manifest_doc={"columns": [
            {"name": "line_item_unblended_cost"},
            {"name": "line_item_usage_amount"},
        ]},
        keys=["exp/exp/metadata/BILLING_PERIOD=2026-01/exp-Manifest.json"],
    )
    cols = cur._manifest_columns()
    assert cols is not None
    assert "line_item_iam_principal" not in cols


def test_manifest_columns_none_when_glue_unreachable(monkeypatch):
    sess = MagicMock()
    sess.client.return_value.get_table.side_effect = RuntimeError("boom")
    monkeypatch.setattr(cur, "get_aws_session", lambda: sess)
    assert cur._manifest_columns() is None


def test_manifest_columns_none_on_bad_location(monkeypatch):
    _patch_session(
        monkeypatch,
        location="not-an-s3-uri",
        manifest_doc={},
        keys=[],
    )
    assert cur._manifest_columns() is None


# ── #784: the health probe must scope to the current billing month ───
#
# The bug: `_probe()` sampled `LIMIT 50` over ALL Bedrock history with
# no date filter / no ordering. CUR only heals the current month
# forward, so on an account with pre-attribution blank history the
# closed-month blanks outvote the attributed current-month rows → the
# sample comes back all-blank → cur_health=principal_blank → $0 spend
# everywhere despite a fully-attributed current month. These assert the
# generated SQL scopes to the current month and surfaces attributed
# rows first, so the probe answers "is the current month attributed?".


def _capture_probe_sql(monkeypatch):
    """Run _probe() with a stubbed Athena client; return the SQL it
    submitted. The query is forced to a quick FAILED→ABSENT so we
    never poll — we only care about the QueryString."""
    captured = {}
    ath = MagicMock()

    def _start(**kw):
        captured["sql"] = kw["QueryString"]
        return {"QueryExecutionId": "qid"}
    ath.start_query_execution.side_effect = _start
    ath.get_query_execution.return_value = {
        "QueryExecution": {"Status": {
            "State": "FAILED",
            "StateChangeReason": "TABLE_NOT_FOUND"}}}

    sess = MagicMock()
    sess.client.return_value = ath
    monkeypatch.setattr(cur, "get_aws_session", lambda: sess)
    cur._probe()
    return captured["sql"]


def test_probe_sql_scopes_to_current_month(monkeypatch):
    sql = _capture_probe_sql(monkeypatch)
    # the date filter is the core #784 fix
    assert "line_item_usage_start_date" in sql
    assert "date_trunc('month', current_date)" in sql


def test_probe_sql_orders_attributed_rows_first(monkeypatch):
    sql = _capture_probe_sql(monkeypatch)
    assert "ORDER BY" in sql
    # non-blank principals must sort before blank/NULL ones
    assert cur._an._PRINCIPAL_COLUMN in sql
    assert "IS NULL" in sql or "= ''" in sql
