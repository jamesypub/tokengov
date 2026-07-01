"""
#657: pr_cost_rollup must not drop spend on days with zero merged PRs.

The row set for team_daily/weekly_metrics was derived solely from
merged-PR keys, so a (team, day) with quota spend but no merged PR
that day produced no row → its spend vanished from the metrics.

Covers:
  - spend-only day → a TeamDailyMetric "all" row carrying the spend,
    prs_merged=0, cycle stats null (the bug).
  - the spend also rolls into the matching TeamWeeklyMetric.
  - regression: a day WITH a merged PR still gets the full spend on
    its "all" row (distribution unchanged).
  - zero-spend quota rows don't emit noise rows.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

import pytest


def _seed_team_with_member(db, team_id, email, handle):
    from db.models import Team, User, LinkedAccount
    db.add(Team(team_id=team_id, name=team_id))
    db.add(User(email=email, status="active", team_id=team_id))
    db.add(LinkedAccount(
        email=email, vendor="github",
        external_handle=handle, linked_by="auto"))


def _spend(db, email, day, usd):
    from db.models import CurUserSpend
    db.add(CurUserSpend(
        email=email, usage_hour=day, model_id="m1",
        input_tokens=0, output_tokens=0, total_tokens=0,
        spend_usd=usd))


def test_spend_only_day_emits_daily_row(clean_db):
    """A team with spend on a day but NO merged PR that day still gets
    a TeamDailyMetric row carrying the spend (prs_merged=0).

    The team DOES have a merged PR on a different day (yesterday), so
    the rollup runs (it skips only when there's no activity at all);
    the bug was that TODAY's spend-only day produced no row."""
    from db.session import get_db
    from db.models import (
        TeamDailyMetric, TeamWeeklyMetric, GithubActivity,
        PrClassification,
    )
    from worker.jobs.pr_cost_rollup import run, _bin_day, _bin_week_start

    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = now - timedelta(days=1)
    with get_db() as db:
        _seed_team_with_member(db, "t1", "alice@x.com", "alice")
        # A merged PR YESTERDAY (so the rollup doesn't skip), but
        # the spend we assert on is TODAY, a day with no merged PR.
        db.add(GithubActivity(
            repo="o/r", pr_number=9, title="old",
            author_login="alice", labels="[]", issue_refs="[]",
            merged_at=yesterday))
        db.add(PrClassification(
            repo="o/r", pr_number=9, pr_class="task",
            classified_by="test"))
        _spend(db, "alice@x.com", today, 42.0)

    run()

    day_key = _bin_day(now)
    with get_db() as db:
        rows = (
            db.query(TeamDailyMetric)
            .filter(
                TeamDailyMetric.team_id == "t1",
                TeamDailyMetric.pr_class == "all",
                TeamDailyMetric.day == day_key,
            ).all()
        )
        assert len(rows) == 1, "spend-only day must emit one all-row"
        row = rows[0]
        assert row.prs_merged == 0
        assert abs(row.spend_usd - 42.0) < 1e-6
        assert row.cycle_median_hours is None

        # And it rolls into the week.
        wk = _bin_week_start(now)
        wrows = (
            db.query(TeamWeeklyMetric)
            .filter(
                TeamWeeklyMetric.team_id == "t1",
                TeamWeeklyMetric.pr_class == "all",
                TeamWeeklyMetric.week_start == wk,
            ).all()
        )
        # The week carries today's spend-only $42. prs_merged here is
        # 1 (yesterday's PR is in the same week) — the point is the
        # spend is NOT dropped, which pre-#657 it was when the day had
        # no PR. (A week with zero PRs anywhere would show prs_merged
        # 0; that's not this fixture.)
        assert len(wrows) == 1
        assert abs(wrows[0].spend_usd - 42.0) < 1e-6


def test_day_with_pr_still_gets_full_spend(clean_db):
    """Regression: a day WITH a merged PR still carries the full
    (team, day) spend on its 'all' row — distribution unchanged."""
    from db.session import get_db
    from db.models import (
        GithubActivity, PrClassification, TeamDailyMetric,
    )
    from worker.jobs.pr_cost_rollup import run, _bin_day

    now = datetime.now(timezone.utc)
    with get_db() as db:
        _seed_team_with_member(db, "t1", "alice@x.com", "alice")
        _spend(db, "alice@x.com", now.date(), 100.0)
        db.add(GithubActivity(
            repo="o/r", pr_number=1, title="x",
            author_login="alice", labels="[]", issue_refs="[]",
            merged_at=now))
        db.add(PrClassification(
            repo="o/r", pr_number=1, pr_class="task",
            classified_by="test"))

    run()

    with get_db() as db:
        allrow = (
            db.query(TeamDailyMetric)
            .filter(
                TeamDailyMetric.team_id == "t1",
                TeamDailyMetric.pr_class == "all",
                TeamDailyMetric.day == _bin_day(now),
            ).one()
        )
        assert allrow.prs_merged == 1
        assert abs(allrow.spend_usd - 100.0) < 1e-6


def test_zero_spend_day_emits_no_noise_row(clean_db):
    """A quota_metrics row with spend_usd=0 (e.g. pricing not yet
    seeded) and no merged PR must NOT create an empty metric row."""
    from db.session import get_db
    from db.models import TeamDailyMetric
    from worker.jobs.pr_cost_rollup import run

    today = datetime.now(timezone.utc).date()
    with get_db() as db:
        _seed_team_with_member(db, "t1", "alice@x.com", "alice")
        _spend(db, "alice@x.com", today, 0.0)

    run()

    with get_db() as db:
        rows = (
            db.query(TeamDailyMetric)
            .filter(TeamDailyMetric.team_id == "t1").all()
        )
        assert rows == [], "zero-spend, zero-PR day must emit no rows"
