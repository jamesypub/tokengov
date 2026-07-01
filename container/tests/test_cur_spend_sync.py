"""
Tests for worker/jobs/cur_spend_sync (#724). The Athena layer
(_run_athena) is stubbed to return synthetic CUR result rows; the
SQLAlchemy writes hit the real Postgres testcontainer. Asserts the
CUR→cur_user_spend population, principal classification/JIT-user,
the replace-current-month behavior, and fail-soft on Athena error.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest

from db.session import get_db
from db.models import (
    CurUserSpend, User, DiscoveredModel, PrincipalModel,
)


def _cols():
    return [
        "principal", "usage_hour", "region", "model_id",
        "usage_type", "spend_usd", "usage_amount",
    ]


# #806: a default input-tokens usage_type so the existing fixtures
# (which predate the token-dimension column) classify deterministically.
# Tests that exercise the dimension split pass their own usage_type.
def _r(principal, hour, region, model_id, spend, amount,
       usage_type="USE1-Model-input-tokens"):
    """Build a stub CUR result row matching _cols() order."""
    return [principal, hour, region, model_id, usage_type, spend, amount]


def _hour(s):
    # Athena date_trunc('hour', ...) string form.
    return s


def _stub(monkeypatch, rows):
    import worker.jobs.cur_spend_sync as job
    monkeypatch.setattr(
        job, "_run_athena", lambda sql: (_cols(), rows))
    return job


_THIS_MONTH = datetime.now(timezone.utc).strftime("%Y-%m")
_HOUR = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:00:00")


def test_sync_populates_and_classifies(clean_db, monkeypatch):
    rows = [
        # human assumed-role (email session)
        _r("arn:aws:sts::123:assumed-role/tg-consumer/alice@test.com",
           _HOUR, "us-east-1",
           "us.anthropic.claude-haiku-4-5-20251001-v1:0", "1.50", "100"),
        # machine role (instance-id session) → #810: keyed on the
        # session name (the instance-id) verbatim, not role:<role>
        _r("arn:aws:sts::123:assumed-role/batch-role/i-0abc",
           _HOUR, "us-west-2",
           "us.anthropic.claude-sonnet-4-6", "4.00", "200"),
    ]
    job = _stub(monkeypatch, rows)
    out = job.run()
    assert out["status"] == "ok"
    assert out["rows"] == 2

    with get_db() as db:
        # human → email principal key
        a = db.query(CurUserSpend).filter(
            CurUserSpend.email == "alice@test.com").one()
        assert a.spend_usd == 1.50
        assert a.region == "us-east-1"
        assert a.billing_period == _THIS_MONTH
        # machine → #810 session-name key (the instance-id), User
        # JIT-created with email None (machine), keyed on the segment
        m = db.query(CurUserSpend).filter(
            CurUserSpend.email == "i-0abc").one()
        assert m.region == "us-west-2"
        mu = db.query(User).filter(
            User.identity_key == "i-0abc").one()
        assert mu.principal_type == "service"
        au = db.query(User).filter(
            User.identity_key == "alice@test.com").one()
        assert au.principal_type == "assumed_role"
        # DiscoveredModel upserted from CUR model ids
        ids = {d.model_id for d in db.query(DiscoveredModel).all()}
        assert "us.anthropic.claude-sonnet-4-6" in ids


def test_resync_replaces_current_month_not_additive(
    clean_db, monkeypatch,
):
    """CUR overwrites the month partition — re-running must REPLACE
    the current month's spend, never add to it. A downward revision
    lowers the stored total."""
    arn = "arn:aws:sts::123:assumed-role/tg-consumer/bob@test.com"
    job = _stub(monkeypatch, [
        _r(arn, _HOUR, "us-east-1", "us.anthropic.claude-sonnet-4-6",
           "10.00", "100"),
    ])
    job.run()
    with get_db() as db:
        assert db.query(CurUserSpend).filter(
            CurUserSpend.email == "bob@test.com").one().spend_usd == 10.00

    # Re-sync with a LOWER value (revised-down month).
    job = _stub(monkeypatch, [
        _r(arn, _HOUR, "us-east-1", "us.anthropic.claude-sonnet-4-6",
           "6.00", "100"),
    ])
    job.run()
    with get_db() as db:
        rows = db.query(CurUserSpend).filter(
            CurUserSpend.email == "bob@test.com").all()
        assert len(rows) == 1           # not stacked
        assert rows[0].spend_usd == 6.00  # revised down, not 16


def test_sync_fail_soft_on_athena_error(clean_db, monkeypatch):
    """Athena unavailable → status 'skipped', no crash, no rows."""
    import worker.jobs.cur_spend_sync as job

    def boom(sql):
        raise RuntimeError("Athena FAILED: TABLE_NOT_FOUND")
    monkeypatch.setattr(job, "_run_athena", boom)

    out = job.run()
    assert out["status"] == "skipped"
    assert out["rows"] == 0
    with get_db() as db:
        assert db.query(CurUserSpend).count() == 0


def test_sync_skips_unparseable_rows(clean_db, monkeypatch):
    """Rows missing principal / hour / model are skipped, not
    crashed on."""
    job = _stub(monkeypatch, [
        _r("", _HOUR, "us-east-1", "m", "1.0", "1"),            # no arn
        _r("arn:aws:sts::1:assumed-role/r/x", "", "us-east-1",  # no hour
           "m", "1.0", "1"),
    ])
    out = job.run()
    assert out["status"] == "ok"
    assert out["rows"] == 0


# ── #785: the billing-period filter must use the partition key ───────
#
# The bug: _query_sql filtered `bill_billing_period_start_date`
# (a timestamp(3) data column) `IN ('2026-06', …)` (varchar) →
# Athena TYPE_MISMATCH → the query FAILED every cycle → fail-soft
# skip → cur_user_spend never populated. _billing_periods already
# emits the 'YYYY-MM' partition-format strings; the filter must hit
# the `billing_period` PARTITION key (matches the format AND prunes
# partitions), never the timestamp column.


def test_query_sql_filters_on_billing_period_partition():
    import worker.jobs.cur_spend_sync as job
    sql = job._query_sql(["2026-05", "2026-06"])
    # filters on the partition key with the YYYY-MM values
    assert "billing_period IN ('2026-05', '2026-06')" in sql
    # NOT the timestamp data column (the TYPE_MISMATCH source)
    assert "bill_billing_period_start_date IN" not in sql


def test_billing_periods_are_yyyy_mm_partition_format():
    """The values fed to the IN-list must be the partition's yyyy-MM
    projection format, so they match `billing_period` exactly."""
    import re
    import worker.jobs.cur_spend_sync as job
    for p in job._billing_periods():
        assert re.fullmatch(r"\d{4}-\d{2}", p), p


# ── #789: in-batch duplicate keys must not crash the spend ingest ────
#
# CUR returns many rows per model_id, and distinct principal ARNs can
# classify to ONE identity_key. The old check-then-add (db .first() +
# add, single flush after the loop) didn't see this session's pending
# adds, so a repeated model_id / identity_key added a duplicate and the
# post-loop flush hit discovered_models_pkey / principal_models_pkey /
# the cur_user_spend unique constraint → the WHOLE transaction rolled
# back → $0 spend in the UI despite attributed CUR. These guard the
# regression with a real Postgres testcontainer (the bug only shows on
# a real flush, never with _run_athena stubbed alone).


def test_duplicate_model_id_in_batch_does_not_crash(clean_db, monkeypatch):
    """A model_id repeated across rows in one sync must insert exactly
    ONE discovered_models row and commit spend — not raise
    IntegrityError on discovered_models_pkey."""
    opus = "us.anthropic.claude-opus-4-6-v1"
    haiku = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    rows = [
        _r("arn:aws:sts::123:assumed-role/tg-consumer/alice@test.com",
           _HOUR, "us-east-1", opus, "1.00", "10"),
        _r("arn:aws:sts::123:assumed-role/tg-consumer/alice@test.com",
           _HOUR, "us-east-1", haiku, "2.00", "20"),
        _r("arn:aws:sts::123:assumed-role/tg-consumer/bob@test.com",
           _HOUR, "us-east-1", haiku, "3.00", "30"),
        # opus AGAIN — same model_id later in the batch (the crash trigger)
        _r("arn:aws:sts::123:assumed-role/tg-consumer/bob@test.com",
           _HOUR, "us-east-1", opus, "4.00", "40"),
    ]
    job = _stub(monkeypatch, rows)
    out = job.run()
    assert out["status"] == "ok"           # did NOT raise + roll back
    with get_db() as db:
        # exactly one discovered_models row per distinct id
        assert db.query(DiscoveredModel).filter(
            DiscoveredModel.model_id == opus).count() == 1
        assert db.query(DiscoveredModel).filter(
            DiscoveredModel.model_id == haiku).count() == 1
        # spend committed (the whole point — not $0)
        assert db.query(CurUserSpend).count() > 0


def test_distinct_arns_same_identity_key_sum_onto_one_row(
    clean_db, monkeypatch,
):
    """#810: distinct raw ARNs that classify to ONE identity_key
    (here, the same email session assumed via TWO different roles —
    last-segment keying collapses both to the email) still sum onto a
    single cur_user_spend row for the same hour/region/model, not a
    unique-constraint collision. (Under the old role:<R> collapse this
    was two instance-ids of one role; that no longer collapses, so the
    cross-role human session is the right same-key case now.)"""
    model = "us.anthropic.claude-sonnet-4-6"
    rows = [
        _r("arn:aws:sts::123:assumed-role/tg-consumer/carol@test.com",
           _HOUR, "us-east-1", model, "5.00", "50"),
        _r("arn:aws:sts::123:assumed-role/tg-install/carol@test.com",
           _HOUR, "us-east-1", model, "7.00", "70"),
    ]
    job = _stub(monkeypatch, rows)
    out = job.run()
    assert out["status"] == "ok"
    with get_db() as db:
        r = db.query(CurUserSpend).filter(
            CurUserSpend.email == "carol@test.com",
            CurUserSpend.model_id == model).all()
        assert len(r) == 1                  # one row, not a collision
        assert r[0].spend_usd == 12.00      # 5 + 7 summed
        # one PrincipalModel for the (identity_key, model_id) pair
        assert db.query(PrincipalModel).filter(
            PrincipalModel.identity_key == "carol@test.com",
            PrincipalModel.model_id == model).count() == 1


# ── #806: populate token columns from CUR usage_type ─────────────────
#
# cur_spend_sync used to write only spend_usd; every token column sat
# at its schema default of 0 (so Activity/C&V showed zeros). CUR
# encodes the token dimension in line_item_usage_type and the count in
# usage_amount. The job must split usage_amount into the right column —
# matching cache BEFORE input, because `cache-read-input-token-count`
# contains the substring `input-token` (the correctness gotcha).


def test_classify_token_dimension_cache_before_input():
    """The substring gotcha: a cache-read/-write usage_type contains
    `input-token`, so it MUST classify as cache, not input."""
    import worker.jobs.cur_spend_sync as job
    f = job._classify_token_dimension
    assert f("USE1-Claude4.5Haiku-input-tokens") == "input"
    assert f("USW2-Claude4.5Haiku-output-tokens-cross-region-global") \
        == "output"
    # the gotcha — contains `input-token` but is a CACHE dimension
    assert f("USW2-Claude4.5Haiku-cache-read-input-token-count"
             "-cross-region-global") == "cache_read"
    assert f("USW2-Claude4.5Haiku-cache-write-input-token-count"
             "-cross-region-global") == "cache_write"
    # model-name-suffixed input still classifies as input
    assert f("USE1-anthropic.claude-opus-4-8-mantle-input-tokens"
             "-global-standard") == "input"
    # not a token dimension → None (don't count as tokens)
    assert f("USE1-SomeOtherCharge") is None
    assert f("") is None


def test_query_sql_groups_by_usage_type():
    """#806: usage_type is in the SELECT + GROUP BY so each row carries
    its token dimension."""
    import worker.jobs.cur_spend_sync as job
    sql = job._query_sql(["2026-06"])
    assert "line_item_usage_type" in sql
    assert "AS usage_type" in sql
    assert "GROUP BY 1, 2, 3, 4, 5" in sql


def test_token_columns_populated_per_dimension(clean_db, monkeypatch):
    """All four dimensions for one (principal,hour,region,model) land in
    their own columns on the single spend row; cache is NOT miscounted
    as input; total_tokens = input + output."""
    arn = "arn:aws:sts::123:assumed-role/tg-consumer/dana@test.com"
    model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    rows = [
        _r(arn, _HOUR, "us-west-2", model, "1.00", "1000",
           "USW2-Claude4.5Haiku-input-tokens"),
        _r(arn, _HOUR, "us-west-2", model, "2.00", "500",
           "USW2-Claude4.5Haiku-output-tokens-cross-region-global"),
        _r(arn, _HOUR, "us-west-2", model, "0.10", "5506",
           "USW2-Claude4.5Haiku-cache-read-input-token-count"
           "-cross-region-global"),
        _r(arn, _HOUR, "us-west-2", model, "0.20", "286",
           "USW2-Claude4.5Haiku-cache-write-input-token-count"
           "-cross-region-global"),
    ]
    job = _stub(monkeypatch, rows)
    out = job.run()
    assert out["status"] == "ok"
    with get_db() as db:
        r = db.query(CurUserSpend).filter(
            CurUserSpend.email == "dana@test.com",
            CurUserSpend.model_id == model).one()  # ONE row, accumulated
        assert r.input_tokens == 1000
        assert r.output_tokens == 500
        # the gotcha: cache-read-input-token-count → cache_read, NOT input
        assert r.cache_read_tokens == 5506
        assert r.cache_write_tokens == 286
        # total = input + output (pre-#720 convention; cache separate)
        assert r.total_tokens == 1500
        # spend still sums across the four dimension rows
        assert abs(r.spend_usd - 3.30) < 1e-9


def test_cache_read_not_miscounted_as_input(clean_db, monkeypatch):
    """Regression for the #806 gotcha in isolation: a row whose only
    dimension is cache-read must leave input_tokens at 0."""
    arn = "arn:aws:sts::123:assumed-role/tg-consumer/erin@test.com"
    model = "us.anthropic.claude-sonnet-4-6"
    rows = [
        _r(arn, _HOUR, "us-east-1", model, "0.05", "9999",
           "USE1-Claude-cache-read-input-token-count-standard"),
    ]
    job = _stub(monkeypatch, rows)
    job.run()
    with get_db() as db:
        r = db.query(CurUserSpend).filter(
            CurUserSpend.email == "erin@test.com").one()
        assert r.cache_read_tokens == 9999
        assert r.input_tokens == 0       # NOT miscounted
        assert r.total_tokens == 0       # no input/output this hour


# ── #950: reconcile principal_arn to the CUR-observed role ───────────
#
# The DB principal_arn must reflect the role CUR ACTUALLY billed for an
# identity, not a hardcoded seed assumption (tg-consumer). On stage a
# user's real traffic flowed through tg-install-from-... but the DB row
# said tg-consumer, so a "blocked" user wasn't actually blocked — the
# deny attached to a role almost no traffic used. The sync reconciles
# each identity's principal_arn to the dominant-by-spend CUR role,
# overwriting a stale seeded value. CUR is ground truth (always wins).


def test_reconciles_principal_arn_to_cur_observed_role(
    clean_db, monkeypatch,
):
    """A user pre-seeded with a stale principal_arn (tg-consumer) whose
    CUR traffic is on a DIFFERENT role (tg-install-from-...) reconciles
    to the CUR-observed role after a sync."""
    email = "frank@test.com"
    with get_db() as db:
        db.add(User(
            email=email, identity_key=email, status="active",
            principal_arn="arn:aws:iam::123:role/tg-consumer",
            principal_type="assumed_role", role_type="iam"))
        db.flush()

    real_role = "tg-install-from-123456789012"
    rows = [
        _r(f"arn:aws:sts::123:assumed-role/{real_role}/{email}",
           _HOUR, "us-east-1", "us.anthropic.claude-sonnet-4-6",
           "0.18", "180"),
    ]
    job = _stub(monkeypatch, rows)
    out = job.run()
    assert out["status"] == "ok"
    assert out["reconciled"] >= 1
    with get_db() as db:
        u = db.query(User).filter(User.identity_key == email).one()
        # reconciled to the role CUR billed, NOT the seeded tg-consumer
        assert u.principal_arn == f"arn:aws:iam::123:role/{real_role}"


def test_multi_role_identity_picks_dominant_by_spend(
    clean_db, monkeypatch,
):
    """When CUR shows an identity under MORE THAN ONE role, the
    reconciled principal_arn is the dominant (highest-spend) role — the
    one real traffic uses — not the last-seen or cheapest. Mirrors the
    stage tg-org-admin case (tg-install-from-... $0.18 vs tg-consumer
    $0.0002 from the seed's own invokes)."""
    email = "tg-org-admin@test.com"
    model = "us.anthropic.claude-sonnet-4-6"
    rows = [
        # negligible spend on tg-consumer (the seed's own invokes)
        _r(f"arn:aws:sts::123:assumed-role/tg-consumer/{email}",
           _HOUR, "us-east-1", model, "0.0002", "4"),
        # the dominant role — real traffic
        _r(f"arn:aws:sts::123:assumed-role/tg-install-from-x/{email}",
           _HOUR, "us-east-1", model, "0.18", "180"),
    ]
    job = _stub(monkeypatch, rows)
    job.run()
    with get_db() as db:
        u = db.query(User).filter(User.identity_key == email).one()
        assert u.principal_arn == \
            "arn:aws:iam::123:role/tg-install-from-x"


def test_single_role_identity_records_its_cur_role(
    clean_db, monkeypatch,
):
    """A freshly-discovered identity (no prior row) ends with the role
    CUR observed — the JIT-created user is self-consistent."""
    email = "grace@test.com"
    rows = [
        _r(f"arn:aws:sts::123:assumed-role/some-app-role/{email}",
           _HOUR, "us-east-1", "us.anthropic.claude-sonnet-4-6",
           "2.00", "20"),
    ]
    job = _stub(monkeypatch, rows)
    job.run()
    with get_db() as db:
        u = db.query(User).filter(User.identity_key == email).one()
        assert u.principal_arn == "arn:aws:iam::123:role/some-app-role"


def test_dominant_tie_break_is_deterministic(clean_db, monkeypatch):
    """Equal spend AND row-count across two roles resolves the same way
    every run (tie-break on the ARN string) — so the stored ARN does
    not thrash between sync ticks on a near-even split."""
    email = "heidi@test.com"
    model = "us.anthropic.claude-sonnet-4-6"
    rows = [
        _r(f"arn:aws:sts::123:assumed-role/role-bbb/{email}",
           _HOUR, "us-east-1", model, "1.00", "10"),
        _r(f"arn:aws:sts::123:assumed-role/role-aaa/{email}",
           _HOUR, "us-east-1", model, "1.00", "10"),
    ]
    # run twice with the rows in different order; same winner both times
    job = _stub(monkeypatch, list(rows))
    job.run()
    with get_db() as db:
        first = db.query(User).filter(
            User.identity_key == email).one().principal_arn
    job = _stub(monkeypatch, list(reversed(rows)))
    job.run()
    with get_db() as db:
        second = db.query(User).filter(
            User.identity_key == email).one().principal_arn
    assert first == second
