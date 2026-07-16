"""
#643: quota_reset is now a retention prune over the per-day
quota_metrics grain. run_daily is gone (daily_tokens dropped —
windowed reads sum the usage_hour range); run_monthly deletes
day-rows older than QUOTA_METRICS_RETENTION_DAYS and leaves
in-window rows untouched.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone


def _seed(email, usage_hour, total=500, spend=1.5):
    from db.session import get_db
    from db.models import CurUserSpend
    with get_db() as db:
        db.add(CurUserSpend(
            email=email, usage_hour=usage_hour, model_id="m1",
            input_tokens=200, output_tokens=300,
            total_tokens=total, spend_usd=spend,
        ))


def test_run_daily_is_gone():
    """#643: run_daily was removed with the daily_tokens column."""
    import worker.jobs.quota_reset as qr
    assert not hasattr(qr, "run_daily")


def test_monthly_prune_deletes_only_old_rows(clean_db, monkeypatch):
    """run_monthly() deletes rows older than the retention window
    and preserves recent ones."""
    monkeypatch.setenv("QUOTA_METRICS_RETENTION_DAYS", "400")
    import importlib
    import worker.jobs.quota_reset as qr
    importlib.reload(qr)  # pick up the env-driven RETENTION_DAYS

    today = datetime.now(timezone.utc).date()
    recent = today - timedelta(days=10)
    ancient = today - timedelta(days=500)   # > 400d retention
    _seed("recent@test.com", recent)
    _seed("ancient@test.com", ancient)

    out = qr.run_monthly()
    assert "deleted 1" in out

    from db.session import get_db
    from db.models import CurUserSpend
    with get_db() as db:
        emails = {r.email for r in db.query(CurUserSpend).all()}
        assert "recent@test.com" in emails
        assert "ancient@test.com" not in emails


def test_monthly_prune_empty_table(clean_db):
    """No rows older than retention → 0 deleted, no error."""
    import worker.jobs.quota_reset as qr
    out = qr.run_monthly()
    assert "deleted 0" in out
