"""
Spend projection: billed CUR + estimated unbilled gap.

Pure-estimator math (avg/p90/peak), the <10-active-hour fallback to
average, the unbilled-hours cap, and the project() composition. The
reconciler off/warn/enforce behavior is in test_deny_reconciler.py.
"""
from __future__ import annotations
import pytest

from db.spend_estimate import (
    hourly_rate, clamp_unbilled_hours, is_low_sample, project,
    normalize_strategy, normalize_enforcement,
    STRATEGY_AVERAGE, STRATEGY_P90, STRATEGY_PEAK,
    UNBILLED_HOURS_CAP, LOW_SAMPLE_MIN_HOURS,
)


def _vals(n, base=10.0):
    """n active hours at a flat base $/hr (>= LOW_SAMPLE for stable
    percentile tests)."""
    return [base] * n


def test_average_is_mean_of_active_hours():
    vals = [10.0, 20.0, 30.0] + [20.0] * 9   # 12 active hours, mean 20
    assert hourly_rate(vals, STRATEGY_AVERAGE) == pytest.approx(
        sum(vals) / len(vals))


def test_peak_is_max():
    vals = [10.0] * 11 + [99.0]
    assert hourly_rate(vals, STRATEGY_PEAK) == 99.0


def test_p90_is_rank_ceil_point9_n():
    # 20 distinct ascending values 1..20; ceil(0.90*20)=18 → 18th value.
    vals = [float(i) for i in range(1, 21)]
    assert hourly_rate(vals, STRATEGY_P90) == 18.0


def test_p90_robust_to_single_freak_hour():
    # 19 hours at $10 + one $1000 freak. ceil(0.90*20)=18 → still $10,
    # NOT the max (that's what makes p90 a position, not the peak).
    vals = [10.0] * 19 + [1000.0]
    assert hourly_rate(vals, STRATEGY_P90) == 10.0
    assert hourly_rate(vals, STRATEGY_PEAK) == 1000.0


def test_low_sample_falls_back_to_average_not_max():
    # < LOW_SAMPLE_MIN_HOURS → p90/peak collapse toward max; we fall
    # back to average so a new/light user isn't projected at the max.
    vals = [10.0, 10.0, 100.0]   # 3 active hours, mean ~40
    mean = sum(vals) / len(vals)
    assert hourly_rate(vals, STRATEGY_P90) == pytest.approx(mean)
    assert hourly_rate(vals, STRATEGY_PEAK) == pytest.approx(mean)
    assert is_low_sample(3) is True
    assert is_low_sample(LOW_SAMPLE_MIN_HOURS) is False


def test_exactly_min_hours_uses_requested_strategy():
    # At the threshold (==MIN), peak is honored (not fallback).
    vals = [10.0] * (LOW_SAMPLE_MIN_HOURS - 1) + [50.0]
    assert hourly_rate(vals, STRATEGY_PEAK) == 50.0


def test_empty_sample_is_zero():
    assert hourly_rate([], STRATEGY_AVERAGE) == 0.0
    assert hourly_rate([0.0, 0.0], STRATEGY_PEAK) == 0.0


def test_idle_hours_filtered_out():
    # Zero/negative hours don't dilute the active-hour rate.
    vals = [0.0] * 5 + [10.0] * 12
    assert hourly_rate(vals, STRATEGY_AVERAGE) == pytest.approx(10.0)


def test_unbilled_hours_cap():
    assert clamp_unbilled_hours(100.0) == float(UNBILLED_HOURS_CAP)
    assert clamp_unbilled_hours(5.0) == 5.0
    assert clamp_unbilled_hours(-3.0) == 0.0
    assert clamp_unbilled_hours(None) == 0.0


def test_project_composition():
    # billed 100 + (rate 10 × 12h) = 220 projected.
    out = project(
        billed_mtd=100.0,
        active_hour_spend=[10.0] * 12,
        unbilled_hours=12.0,
        strategy=STRATEGY_AVERAGE)
    assert out["billed"] == 100.0
    assert out["rate"] == pytest.approx(10.0)
    assert out["estimated"] == pytest.approx(120.0)
    assert out["projected"] == pytest.approx(220.0)
    assert out["unbilled_hours"] == 12.0
    assert out["strategy"] == STRATEGY_AVERAGE
    assert out["low_sample"] is False


def test_project_caps_unbilled_hours():
    out = project(
        billed_mtd=0.0,
        active_hour_spend=[10.0] * 12,
        unbilled_hours=999.0,        # way past the cap
        strategy=STRATEGY_AVERAGE)
    assert out["unbilled_hours"] == float(UNBILLED_HOURS_CAP)
    assert out["estimated"] == pytest.approx(10.0 * UNBILLED_HOURS_CAP)


def test_project_low_sample_flag_and_fallback():
    out = project(
        billed_mtd=0.0,
        active_hour_spend=[10.0, 100.0],   # 2 hrs → low sample
        unbilled_hours=10.0,
        strategy=STRATEGY_PEAK)            # requested peak…
    assert out["low_sample"] is True
    # …but rate fell back to average (55), not peak (100).
    assert out["rate"] == pytest.approx(55.0)
    # strategy field reflects the REQUESTED strategy (what the admin chose)
    assert out["strategy"] == STRATEGY_PEAK


def test_normalize_strategy_and_enforcement():
    assert normalize_strategy("P90") == STRATEGY_P90
    assert normalize_strategy("bogus") == STRATEGY_AVERAGE
    assert normalize_strategy(None) == STRATEGY_AVERAGE
    assert normalize_enforcement("ENFORCE") == "enforce"
    assert normalize_enforcement("bogus") == "off"
    assert normalize_enforcement(None) == "off"
