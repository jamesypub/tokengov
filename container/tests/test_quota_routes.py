"""
Tests for api/routes/quota.py — /api/usage, /api/summary,
/api/activity. Memory feedback_verify_list_endpoint_aggregates:
list endpoints have a habit of defaulting aggregates (spend,
tokens) to 0 even when the SQL is correct elsewhere — these
tests pin the actual numeric values, not just shape.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    """Per-test client: reuses the shared Postgres
    testcontainer (pg_url) but seeds a fresh admin via
    clean_db so every test starts from a known state."""
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"),
    )

    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))

    from api.main import app
    with TestClient(app) as c:
        yield c


def _seed_metric(email, model, spend, tokens=1000):
    from db.session import get_db
    from db.models import CurUserSpend
    usage_hour = datetime.now(timezone.utc).date()  # #643
    with get_db() as db:
        db.add(CurUserSpend(
            email=email, usage_hour=usage_hour, model_id=model,
            input_tokens=tokens // 2,
            output_tokens=tokens // 2,
            total_tokens=tokens,
            spend_usd=spend,
        ))


def _seed_user(email, cap):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(email=email, status="active", cap_usd=cap))


def _seed_principal(email, principal_type, principal_arn):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            u = User(email=email, status="active")
            db.add(u)
        u.principal_type = principal_type
        u.principal_arn = principal_arn


def test_usage_returns_per_user_per_model_rows(client):
    """/api/usage flattens per (email, model). One row in,
    one row out, with the spend value preserved."""
    _seed_user("a@test.com", cap=10.0)
    _seed_metric("a@test.com", "haiku", spend=1.25,
                 tokens=2000)

    r = client.get("/api/usage")
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["email"] == "a@test.com"
    assert row["model"] == "haiku"
    assert row["spend_usd"] == 1.25
    assert row["total_tokens"] == 2000
    assert "team_id" in row


def test_usage_rows_carry_managed_flag(client):
    """#436: usage rows must carry `managed` so the Activity
    page can filter unmanaged principals. A caller through
    tg-consumer is managed; through another role is not;
    a metric with no matching User row is unmanaged."""
    _seed_principal(
        "managed@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer")
    _seed_metric("managed@test.com", "haiku", spend=1.0)
    _seed_principal(
        "bypass@test.com", "assumed_role",
        "arn:aws:iam::123:role/AcmeEng")
    _seed_metric("bypass@test.com", "haiku", spend=1.0)
    # Metric with no User row (outer join → null principal).
    _seed_metric("orphan@test.com", "haiku", spend=1.0)

    rows = {r["email"]: r for r in
            client.get("/api/usage").json()["rows"]}
    assert rows["managed@test.com"]["managed"] is True
    assert rows["bypass@test.com"]["managed"] is False
    assert rows["orphan@test.com"]["managed"] is False


def _set_status(email, status):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.query(User).filter(User.email == email).update(
            {"status": status})


def test_summary_approaching_cap_and_total_spend(client):
    """/api/summary: approaching_cap_count = non-blocked users at
    >=90% of cap; total_spend_usd = sum of in-scope MTD spend;
    active_users = distinct in-window emails. (exceeded_count /
    warning_count are gone — replaced by the Users-card fields.)"""
    _seed_user("ok@test.com",   cap=10.0)
    _seed_user("near@test.com", cap=10.0)
    _seed_user("over@test.com", cap=10.0)
    _seed_metric("ok@test.com",   "m", spend=1.0)
    _seed_metric("near@test.com", "m", spend=9.5)   # 95% → approaching
    _seed_metric("over@test.com", "m", spend=11.0)  # 110% → approaching

    body = client.get("/api/summary").json()
    assert body["active_users"] == 3
    # >=0.90 catches both 95% and 110% (no separate exceeded bucket)
    assert body["approaching_cap_count"] == 2
    assert body["blocked_count"] == 0
    assert abs(body["total_spend_usd"] - 21.5) < 1e-6
    # old fields are gone
    assert "exceeded_count" not in body
    assert "warning_count" not in body


def test_summary_approaching_cap_threshold_is_0_90(client):
    """An 89% user is NOT approaching; a 90% user IS (the boundary)."""
    _seed_user("under@test.com", cap=10.0)
    _seed_user("at@test.com",    cap=10.0)
    _seed_metric("under@test.com", "m", spend=8.9)   # 89% → no
    _seed_metric("at@test.com",    "m", spend=9.0)   # 90% → yes
    body = client.get("/api/summary").json()
    assert body["approaching_cap_count"] == 1


def test_summary_blocked_count_from_status_incl_no_usage(client):
    """blocked_count is sourced from persisted User.status over the
    role-scoped MEMBERSHIP — so a blocked user with NO usage rows this
    window (stopped invoking / force-blocked under cap) is still
    counted, the #1191 bug fix."""
    _seed_user("active@test.com",  cap=10.0)
    _seed_user("blk@test.com",     cap=10.0)
    _seed_user("forced@test.com",  cap=10.0)
    _seed_metric("active@test.com", "m", spend=1.0)
    # blk has usage this window; forced does NOT (no metric row)
    _seed_metric("blk@test.com",    "m", spend=2.0)
    _set_status("blk@test.com",    "blocked")
    _set_status("forced@test.com", "force_blocked")

    body = client.get("/api/summary").json()
    # both blocked users counted, including the no-usage force_blocked
    assert body["blocked_count"] == 2
    # active_users counts only in-window emails (forced has none)
    assert body["active_users"] == 2


def test_summary_blocked_excluded_from_approaching_cap(client):
    """A blocked user at >=90% of cap counts in blocked_count and NOT
    in approaching_cap_count (no double-count, mutual exclusivity)."""
    _seed_user("hot@test.com", cap=10.0)
    _seed_metric("hot@test.com", "m", spend=9.5)   # 95% of cap
    _set_status("hot@test.com", "blocked")
    body = client.get("/api/summary").json()
    assert body["blocked_count"] == 1
    assert body["approaching_cap_count"] == 0


def test_summary_active_users_is_distinct_emails(client):
    """active_users is distinct emails seen in
    quota_metrics, not row count. Two model rows for one
    user → 1 active_user."""
    _seed_user("u@test.com", cap=10.0)
    _seed_metric("u@test.com", "haiku",  spend=0.5)
    _seed_metric("u@test.com", "sonnet", spend=1.5)

    r = client.get("/api/summary")
    assert r.json()["active_users"] == 1


def test_activity_groups_models_under_user(client):
    """/api/activity: two model rows for one user collapse
    into one user-level entry with both models listed and
    spend summed."""
    _seed_user("g@test.com", cap=10.0)
    _seed_metric("g@test.com", "haiku",  spend=1.0,
                 tokens=500)
    _seed_metric("g@test.com", "sonnet", spend=2.0,
                 tokens=300)

    r = client.get("/api/activity")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    user = rows[0]
    assert user["email"] == "g@test.com"
    assert {m["model_id"] for m in user["models"]} == {
        "haiku", "sonnet"}
    assert abs(user["spend_usd"] - 3.0) < 1e-6
    assert user["total_tokens"] == 800


def test_team_filter_excludes_other_team_users(client):
    """/api/usage?team=<id> must filter to that team's
    users; users on other teams must NOT appear."""
    from db.session import get_db
    from db.models import User, Team

    with get_db() as db:
        db.add(Team(team_id="t1", name="Team1"))
        db.add(Team(team_id="t2", name="Team2"))
    _seed_user("on@test.com",  cap=10.0)
    _seed_user("off@test.com", cap=10.0)
    with get_db() as db:
        db.query(User).filter(User.email == "on@test.com"
            ).update({"team_id": "t1"})
        db.query(User).filter(User.email == "off@test.com"
            ).update({"team_id": "t2"})
    _seed_metric("on@test.com",  "m", spend=1.0)
    _seed_metric("off@test.com", "m", spend=2.0)

    r = client.get("/api/usage?team=t1")
    emails = {row["email"] for row in r.json()["rows"]}
    assert emails == {"on@test.com"}


def test_usage_returns_team_id_per_row(client):
    """/api/usage rows must carry the user's team_id so
    the Activity table can render the Team column. Users
    without a team get team_id=None (UI shows em-dash)."""
    from db.session import get_db
    from db.models import User, Team

    with get_db() as db:
        db.add(Team(team_id="alpha", name="Alpha"))
    _seed_user("alpha@test.com", cap=10.0)
    _seed_user("nobody@test.com", cap=10.0)
    with get_db() as db:
        db.query(User).filter(
            User.email == "alpha@test.com"
        ).update({"team_id": "alpha"})
    _seed_metric("alpha@test.com", "m", spend=1.0)
    _seed_metric("nobody@test.com", "m", spend=2.0)

    r = client.get("/api/usage")
    assert r.status_code == 200
    by_email = {row["email"]: row for row in r.json()["rows"]}
    assert by_email["alpha@test.com"]["team_id"] == "alpha"
    assert by_email["nobody@test.com"]["team_id"] is None


def test_summary_empty_metrics_returns_zeros(client):
    """No quota_metrics rows → all-zero summary, not 500."""
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["active_users"] == 0
    assert body["approaching_cap_count"] == 0
    assert body["blocked_count"] == 0
    assert body["total_spend_usd"] == 0


# ── #643 per-day window param ─────────────────────────────

def _seed_day(email, usage_hour, spend, model="m1"):
    from db.session import get_db
    from db.models import CurUserSpend
    with get_db() as db:
        db.add(CurUserSpend(
            email=email, usage_hour=usage_hour, model_id=model,
            input_tokens=0, output_tokens=0,
            total_tokens=0, spend_usd=spend,
        ))


def test_usage_window_sums(client):
    """#643: /api/usage?window=7d|30d|mtd sum the right
    usage_hour ranges. Seed rows across a 40-day span; each
    window's total reflects exactly the days it covers."""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).date()
    # one $1 row per day for the last 40 days (incl. today)
    for i in range(40):
        _seed_day("u@test.com", today - timedelta(days=i), 1.0)

    def total(window):
        r = client.get(f"/api/usage?window={window}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["window"] == window
        return sum(row["spend_usd"] for row in body["rows"])

    # 7d = today + 6 prior = 7 rows = $7
    assert total("7d") == 7.0
    # 30d = today + 29 prior = 30 rows = $30
    assert total("30d") == 30.0
    # mtd = first-of-month..today = today.day rows (<= 31, <= 40)
    mtd = total("mtd")
    assert mtd == float(today.day)


def test_usage_default_window_is_mtd(client):
    """#643: no window param defaults to mtd (preserves prior
    month-to-date behavior)."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    _seed_day("u@test.com", today, 2.5)
    r = client.get("/api/usage")
    body = r.json()
    assert body["window"] == "mtd"
    assert sum(row["spend_usd"] for row in body["rows"]) == 2.5


# ── spend-estimate warn mode ────────────────────────────────────────


def _seed_hours_q(email, model, per_hour, n_hours, end=None):
    """Seed n_hours of billed CurUserSpend at per_hour $, one row per
    hour ending `end` (default now), so the projection has a stable
    trailing-window rate AND an unbilled gap."""
    from datetime import timedelta
    from db.session import get_db
    from db.models import CurUserSpend
    if end is None:
        end = datetime.now(timezone.utc)
    with get_db() as db:
        for i in range(n_hours):
            db.add(CurUserSpend(
                email=email, model_id=model,
                usage_hour=end - timedelta(hours=i + 1),
                input_tokens=0, output_tokens=0, total_tokens=0,
                spend_usd=per_hour))


def _set_enf(strategy, enforcement):
    from db.session import get_db
    from db.org_config import (
        set_spend_estimate_strategy, set_spend_estimate_enforcement)
    with get_db() as db:
        set_spend_estimate_strategy(db, strategy)
        set_spend_estimate_enforcement(db, enforcement)


def test_warn_flag_set_when_estimate_crosses_cap(client):
    """billed alone < cap but billed + estimated >= cap → the row
    carries projected_over_cap=true (the warn-mode signal). Verified on
    /api/users (the Users-page source) and /api/usage (Activity)."""
    from datetime import timedelta
    _set_enf("average", "warn")
    # billed 12h × $5 = $60 < cap 100; history ends 10h ago so the
    # unbilled gap × $5/hr (~$50) pushes projected to ~$110 >= 100.
    _seed_user("warn@test.com", cap=100.0)
    _seed_hours_q("warn@test.com", "m", per_hour=5.0, n_hours=12,
                  end=datetime.now(timezone.utc) - timedelta(hours=10))

    users = {u["email"]: u for u in
             client.get("/api/users").json()["users"]}
    row = users["warn@test.com"]
    assert row["billed"] < 100.0          # billed alone under cap
    assert row["projected"] >= 100.0      # estimate crosses it
    assert row["projected_over_cap"] is True

    usage = client.get("/api/usage").json()
    assert usage["estimate_enforcement"] == "warn"
    urow = next(r for r in usage["rows"]
                if r["email"] == "warn@test.com")
    assert urow["projected_over_cap"] is True


def test_warn_flag_false_when_already_billed_over(client):
    """An already-billed-over user is a normal billed-over case, NOT an
    estimate warning → projected_over_cap stays false."""
    _set_enf("average", "warn")
    _seed_user("billedover@test.com", cap=10.0)
    _seed_metric("billedover@test.com", "m", spend=25.0)  # billed > cap
    users = {u["email"]: u for u in
             client.get("/api/users").json()["users"]}
    assert users["billedover@test.com"]["projected_over_cap"] is False


def test_warn_flag_false_when_projection_under_cap(client):
    """billed + estimated still under cap → no warning."""
    from datetime import timedelta
    _set_enf("average", "warn")
    _seed_user("under@test.com", cap=100000.0)
    _seed_hours_q("under@test.com", "m", per_hour=1.0, n_hours=12,
                  end=datetime.now(timezone.utc) - timedelta(hours=10))
    users = {u["email"]: u for u in
             client.get("/api/users").json()["users"]}
    assert users["under@test.com"]["projected_over_cap"] is False


def test_usage_exposes_enforcement_mode(client):
    """/api/usage carries estimate_enforcement so the UI can gate the
    warn marker; default off."""
    assert client.get("/api/usage").json()["estimate_enforcement"] \
        == "off"
    _set_enf("average", "enforce")
    assert client.get("/api/usage").json()["estimate_enforcement"] \
        == "enforce"
