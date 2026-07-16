"""Unit tests for api.idc_enforcement — the pure classifier that
decides whether a governed IDC user's deny is VERIFIABLY enforced.
No IAM / DB — pure functions only."""
from api import idc_enforcement as ie


class TestIdcRoleName:
    def test_path_form_arn(self):
        arn = ("arn:aws:iam::123456789012:role/aws-reserved/"
               "sso.amazonaws.com/AWSReservedSSO_BedrockDeveloper_abc123")
        assert ie.idc_role_name(arn) == (
            "AWSReservedSSO_BedrockDeveloper_abc123")

    def test_collapsed_form_arn(self):
        arn = ("arn:aws:iam::123456789012:role/"
               "AWSReservedSSO_BedrockDeveloper_abc123")
        assert ie.idc_role_name(arn) == (
            "AWSReservedSSO_BedrockDeveloper_abc123")

    def test_non_idc_role_returns_none(self):
        assert ie.idc_role_name(
            "arn:aws:iam::123456789012:role/tg-consumer") is None

    def test_none_and_blank(self):
        assert ie.idc_role_name(None) is None
        assert ie.idc_role_name("") is None
        assert ie.idc_role_name("not-an-arn") is None


class TestClassify:
    def test_enforced_here_wins(self):
        # Deny attached to the user's OWN SSO role → strongest proof.
        assert ie.classify(
            sso_role_attached=True,
            consumer_attached=False,
            consumer_trust_wired=False,
        ) == ie.ENFORCED_HERE

    def test_enforced_here_beats_consumer(self):
        assert ie.classify(
            sso_role_attached=True,
            consumer_attached=True,
            consumer_trust_wired=True,
        ) == ie.ENFORCED_HERE

    def test_enforced_via_consumer(self):
        assert ie.classify(
            sso_role_attached=False,
            consumer_attached=True,
            consumer_trust_wired=True,
        ) == ie.ENFORCED_VIA_CONSUMER

    def test_consumer_attached_but_trust_not_wired_is_pending(self):
        # Deny on tg-consumer but this user can't assume it → not
        # enforced for THEM.
        assert ie.classify(
            sso_role_attached=False,
            consumer_attached=True,
            consumer_trust_wired=False,
        ) == ie.PENDING

    def test_pending_when_neither_enforced(self):
        # demo0's real state: deny only on tg-consumer, this user's SSO
        # role bare, and they don't assume tg-consumer.
        assert ie.classify(
            sso_role_attached=False,
            consumer_attached=False,
            consumer_trust_wired=False,
        ) == ie.PENDING

    def test_unknown_when_sso_read_failed(self):
        # A failed read of the deciding fact must NOT masquerade as
        # pending — report unknown.
        assert ie.classify(
            sso_role_attached=None,
            consumer_attached=False,
            consumer_trust_wired=False,
        ) == ie.UNKNOWN

    def test_unknown_when_consumer_read_failed(self):
        assert ie.classify(
            sso_role_attached=False,
            consumer_attached=None,
            consumer_trust_wired=True,
        ) == ie.UNKNOWN

    def test_enforced_here_short_circuits_even_on_unknown_consumer(self):
        # Own-role attachment is decisive; a failed consumer read
        # doesn't downgrade a confirmed enforced-here to unknown.
        assert ie.classify(
            sso_role_attached=True,
            consumer_attached=None,
            consumer_trust_wired=False,
        ) == ie.ENFORCED_HERE


class TestIsEnforced:
    def test_enforced_states(self):
        assert ie.is_enforced(ie.ENFORCED_HERE) is True
        assert ie.is_enforced(ie.ENFORCED_VIA_CONSUMER) is True

    def test_non_enforced_states(self):
        assert ie.is_enforced(ie.PENDING) is False
        assert ie.is_enforced(ie.UNKNOWN) is False
