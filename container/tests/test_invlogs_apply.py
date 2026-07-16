"""Unit tests for the Bedrock invocation-logging apply layer
(api.invlogs_apply) + the region catalog (db.invlogs_config) region
validation. Pure / mocked bedrock — no live AWS.

Load-bearing rules under test: ENABLE only when logging isn't already
on (never clobber the singleton); DISABLE only when the live sink is
OUR bucket (fail closed); the pure plan() diff.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from api import invlogs_apply as ia
from db import invlogs_config as cfg


def _bedrock(live_bucket=None, raise_get=False):
    """A mock bedrock client whose Get reports the given live sink."""
    br = MagicMock()
    if raise_get:
        br.get_model_invocation_logging_configuration.side_effect = \
            RuntimeError("unreadable")
    elif live_bucket is None:
        br.get_model_invocation_logging_configuration.return_value = {}
    else:
        br.get_model_invocation_logging_configuration.return_value = {
            "loggingConfig": {"s3Config": {"bucketName": live_bucket}}}
    return br


class TestEnableRegion:
    def test_enables_when_no_config_present(self):
        br = _bedrock(live_bucket=None)
        out = ia.enable_region(br, "tg-bedrock-invlogs-us-east-1-1")
        assert out == ia.APPLIED_ENABLED
        br.put_model_invocation_logging_configuration.assert_called_once()
        # S3-only, no largeDataDeliveryS3Config (invalid for S3 sink)
        kw = br.put_model_invocation_logging_configuration.call_args.kwargs
        cfg_arg = kw["loggingConfig"]
        assert "largeDataDeliveryS3Config" not in cfg_arg
        assert cfg_arg["s3Config"]["bucketName"].endswith("us-east-1-1")

    def test_does_not_clobber_a_different_existing_config(self):
        # A customer's own config already points elsewhere → leave it.
        br = _bedrock(live_bucket="someone-elses-bucket")
        out = ia.enable_region(br, "tg-bedrock-invlogs-us-east-1-1")
        assert out == ia.ALREADY_ENABLED
        br.put_model_invocation_logging_configuration.assert_not_called()

    def test_noop_when_already_ours(self):
        b = "tg-bedrock-invlogs-us-east-1-1"
        br = _bedrock(live_bucket=b)
        assert ia.enable_region(br, b) == ia.NOOP
        br.put_model_invocation_logging_configuration.assert_not_called()

    def test_failed_on_put_error(self):
        br = _bedrock(live_bucket=None)
        br.put_model_invocation_logging_configuration.side_effect = \
            RuntimeError("throttled")
        assert ia.enable_region(br, "b") == ia.FAILED

    def test_text_off_passes_false(self):
        br = _bedrock(live_bucket=None)
        ia.enable_region(br, "b", text_on=False)
        kw = br.put_model_invocation_logging_configuration.call_args.kwargs
        assert kw["loggingConfig"]["textDataDeliveryEnabled"] is False


class TestDisableRegion:
    def test_disables_only_our_bucket(self):
        b = "tg-bedrock-invlogs-us-east-1-1"
        br = _bedrock(live_bucket=b)
        assert ia.disable_region(br, b) == ia.APPLIED_DISABLED
        br.delete_model_invocation_logging_configuration.assert_called_once()

    def test_leaves_a_foreign_config(self):
        br = _bedrock(live_bucket="someone-elses-bucket")
        assert ia.disable_region(br, "tg-bedrock-invlogs-x") == ia.NOT_OURS
        br.delete_model_invocation_logging_configuration.assert_not_called()

    def test_noop_when_already_off(self):
        br = _bedrock(live_bucket=None)
        assert ia.disable_region(br, "b") == ia.NOOP


class TestPlan:
    def test_enable_new_disable_orphan(self):
        p = ia.plan(
            [{"region": "us-east-1", "bucket": "b1", "enabled": True}],
            {"eu-west-1"})   # eu-west-1 live but not desired → orphan
        assert [e["region"] for e in p["enable"]] == ["us-east-1"]
        assert [e["region"] for e in p["disable"]] == ["eu-west-1"]

    def test_explicit_disable_not_double_listed(self):
        p = ia.plan(
            [{"region": "eu-west-1", "bucket": "b2", "enabled": False}],
            {"eu-west-1"})
        # exactly once, from the explicit-disable branch
        assert [e["region"] for e in p["disable"]] == ["eu-west-1"]

    def test_already_live_and_desired_is_noop(self):
        p = ia.plan(
            [{"region": "us-east-1", "bucket": "b1", "enabled": True}],
            {"us-east-1"})
        assert p["enable"] == [] and p["disable"] == []


class TestCatalogRegionValidation:
    def test_normalize_rejects_bad_region_and_lowercases(self):
        # _normalize is the pure region validator both get/set use.
        assert cfg._normalize({"region": "not-a-region"}, "1") is None
        assert cfg._normalize({"region": ""}, "1") is None
        assert cfg._normalize("notadict", "1") is None
        assert cfg._normalize({"region": "US-EAST-1"}, "1")["region"] \
            == "us-east-1"

    def test_bucket_is_derived_never_trusted(self):
        norm = cfg._normalize(
            {"region": "us-east-1", "bucket": "attacker-bucket"}, "999")
        assert norm["bucket"] == "tg-bedrock-invlogs-us-east-1-999"
