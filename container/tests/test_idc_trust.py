"""Tests for api/idc_trust — the pure trust-policy functions that wire
an IDC dev's SSO role into tg-consumer's AssumeRolePolicyDocument
(#1065). No DB / AWS."""
import pytest

from api import idc_trust


# ── permset_arnlike: derive the churn-safe ArnLike pattern ──

@pytest.mark.parametrize("arn,expected", [
    # path-form ARN (what the classifier now stores) → wildcard suffix
    (
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_BedrockDeveloper_0123456789abcdef",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_BedrockDeveloper_*",
    ),
    # collapsed bare form (older rows) → same path-form wildcard
    (
        "arn:aws:iam::123:role/AWSReservedSSO_BedrockDeveloper_abc123",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_BedrockDeveloper_*",
    ),
    # permission-set name containing underscores: anchor on the
    # TRAILING _<suffix>, keep the rest of the name intact.
    (
        "arn:aws:iam::123:role/AWSReservedSSO_Data_Eng_Admin_deadbeef",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Data_Eng_Admin_*",
    ),
    # non-IDC role → None (nothing to wire)
    ("arn:aws:iam::123:role/tg-consumer", None),
    (None, None),
    ("", None),
])
def test_permset_arnlike(arn, expected):
    assert idc_trust.permset_arnlike(arn) == expected


def test_two_suffixes_one_permset_collapse():
    """Two users on the same permission set (different suffixes) map to
    the SAME ArnLike pattern → one trust entry."""
    a = idc_trust.permset_arnlike(
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_aaa")
    b = idc_trust.permset_arnlike(
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_bbb")
    assert a == b


# ── add_trust / has_trust / remove_trust ──

def _empty_doc():
    return {"Version": "2012-10-17", "Statement": []}


def test_add_trust_is_idempotent():
    arnlike = ("arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
               "AWSReservedSSO_Dev_*")
    doc, changed = idc_trust.add_trust(_empty_doc(), arnlike, "123")
    assert changed is True
    assert idc_trust.has_trust(doc, arnlike)
    # added statement shape: Allow sts:AssumeRole, root principal,
    # ArnLike on aws:PrincipalArn.
    s = doc["Statement"][-1]
    assert s["Effect"] == "Allow"
    assert s["Action"] == "sts:AssumeRole"
    assert s["Principal"]["AWS"] == "arn:aws:iam::123:root"
    assert s["Condition"]["ArnLike"]["aws:PrincipalArn"] == arnlike
    assert s["Sid"].startswith("TgGovernIdc")
    # re-add → no change, still one entry
    doc2, changed2 = idc_trust.add_trust(doc, arnlike, "123")
    assert changed2 is False
    assert sum(1 for x in doc2["Statement"]
               if str(x.get("Sid", "")).startswith("TgGovernIdc")) == 1


def test_remove_trust_only_touches_govern_statements():
    """remove_trust drops the tg-govern statement, never an
    install-time trust (no TgGovernIdc Sid)."""
    arnlike = ("arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
               "AWSReservedSSO_Dev_*")
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            # an install-time SSO trust (#1064 shape) — same ArnLike but
            # NO TgGovernIdc Sid: must be preserved.
            {
                "Sid": "InstallSso",
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::123:root"},
                "Action": "sts:AssumeRole",
                "Condition": {"ArnLike": {"aws:PrincipalArn": arnlike}},
            },
        ],
    }
    doc, _ = idc_trust.add_trust(doc, arnlike, "123")  # tg-govern entry
    new, changed = idc_trust.remove_trust(doc, arnlike)
    assert changed is True
    sids = [s.get("Sid") for s in new["Statement"]]
    assert "InstallSso" in sids                    # install trust kept
    assert not any(str(s).startswith("TgGovernIdc") for s in sids)


def test_remove_trust_noop_when_absent():
    arnlike = ("arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
               "AWSReservedSSO_Dev_*")
    new, changed = idc_trust.remove_trust(_empty_doc(), arnlike)
    assert changed is False
