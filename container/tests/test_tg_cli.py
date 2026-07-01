"""tg CLI — Layer 1 (unit) + Layer 2 (real-CLI dry-run) tests (#487).

L1: the 7-Q flow, --non-interactive arg parsing, ~/.tg/config.json
resume + idempotency + secret-scrubbing, cert 3-way choice — all with
NO real AWS (no boto3, the wizard maps to env only).

L2: run the ACTUAL `tg` CLI binary in --dry-run mode and assert it
validates inputs, prints the confirm screen, and STOPS before
mutating (the test-the-real-artifact rule — a packaging/argparse bug
that import-level unit tests miss shows up here).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# tg_cli lives under scripts/python/ — add it to the path for L1.
_REPO = Path(__file__).resolve().parents[2]
_PYDIR = _REPO / "scripts" / "python"
sys.path.insert(0, str(_PYDIR))

from tg_cli import config, validate as V  # noqa: E402
from tg_cli.prompts import PromptAbort, Resolver  # noqa: E402
from tg_cli.wizard import (  # noqa: E402
    AUTH_COGNITO,
    AUTH_OKTA,
    CERT_EXISTING,
    CERT_PLAINTEXT,
    CERT_SELFSIGNED,
    VPC_CREATE,
    VPC_EXISTING,
    cert_scheme,
    redirect_uri,
    run_questions,
    to_env,
)

TG = _REPO / "scripts" / "tg"


# ────────────────────────── L1: validators ──────────────────────────

@pytest.mark.parametrize("val,ok", [
    ("us-east-1", True), ("eu-west-2", True), ("nonsense", False),
])
def test_region_validator(val, ok):
    assert (V.region(val) is None) == ok


@pytest.mark.parametrize("val,ok", [
    ("203.0.113.0/24", True),
    ("203.0.113.0/24,10.0.0.0/8", True),
    ("", False),                       # fail-closed: no empty allowlist
    ("1.2.3.4/8,5.6.7.8/8,9.9.9.9/8,1.1.1.1/8,2.2.2.2/8", False),  # >4
    ("not-a-cidr", False),
])
def test_cidr_validator(val, ok):
    assert (V.cidrs(val) is None) == ok


# #875: 0.0.0.0/0 is no longer an unconditional reject. It depends on
# the login wall + the TG_REQUIRE_IP_ALLOWLIST policy flag, kept in
# lock-step with tg-ecs-install.sh.
def test_open_all_allowed_when_login_on_and_policy_off(monkeypatch):
    monkeypatch.delenv("TG_REQUIRE_IP_ALLOWLIST", raising=False)
    monkeypatch.delenv("TG_AUTH_REQUIRE_LOGIN", raising=False)
    # default: login on (var unset ≠ "0"), policy off → permitted
    assert V.cidrs("0.0.0.0/0") is None


def test_open_all_refused_when_login_off(monkeypatch):
    monkeypatch.delenv("TG_REQUIRE_IP_ALLOWLIST", raising=False)
    monkeypatch.setenv("TG_AUTH_REQUIRE_LOGIN", "0")
    err = V.cidrs("0.0.0.0/0")
    assert err is not None and "login wall off" in err


def test_open_all_rejected_when_strict_policy_on(monkeypatch):
    monkeypatch.setenv("TG_REQUIRE_IP_ALLOWLIST", "1")
    monkeypatch.setenv("TG_AUTH_REQUIRE_LOGIN", "1")  # on, but policy wins
    err = V.cidrs("0.0.0.0/0")
    assert err is not None and "TG_REQUIRE_IP_ALLOWLIST" in err


# ──────────── #875: interactive ingress-CIDR choice set ─────────────
# These drive run_questions interactively with NO scripted/supplied
# ingress_cidrs, so the _ask_cidrs choice menu runs. detect_public_ip
# is monkeypatched (no real egress in CI).

def _scripted_no_cidrs(**overrides):
    s = _scripted(**overrides)
    s.pop("ingress_cidrs", None)
    return s


class _ChoicePicker(Resolver):
    """Answers the ingress_choice menu with a substring match; answers the
    manual ingress_cidrs fallback question with `manual_cidr` (so the menu
    short-circuit isn't tripped by a scripted ingress_cidrs). Defers every
    other key to the scripted base. Records the offered choices in
    .seen_choices for menu-content assertions."""
    def __init__(self, want, manual_cidr=None, **kw):
        super().__init__(**kw)
        self._want = want
        self._manual = manual_cidr
        self.seen_choices = None

    def ask(self, q):
        if q.key == "ingress_choice":
            self.seen_choices = list(q.choices)
            for c in q.choices:
                if self._want in c:
                    return c
            raise AssertionError(f"no choice matched {self._want!r}: {q.choices}")
        if q.key == "ingress_cidrs" and self._manual is not None:
            return self._checked(q, self._manual)
        return super().ask(q)


def test_cidr_detect_default_prefills_slash32(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "detect_public_ip", lambda *a, **k: "203.0.113.7")
    monkeypatch.delenv("TG_REQUIRE_IP_ALLOWLIST", raising=False)
    r = _ChoicePicker("Detected", interactive=True,
                      scripted=_scripted_no_cidrs())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["ingress_cidrs"] == "203.0.113.7/32"


def test_cidr_no_egress_falls_back_to_manual(monkeypatch):
    # detect returns None (no egress). With login on + policy off the menu
    # still offers custom + open-all; pick custom → the manual question,
    # answered here via manual_cidr.
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "detect_public_ip", lambda *a, **k: None)
    monkeypatch.delenv("TG_REQUIRE_IP_ALLOWLIST", raising=False)
    r = _ChoicePicker("Custom", manual_cidr="10.0.0.0/8", interactive=True,
                      scripted=_scripted_no_cidrs())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["ingress_cidrs"] == "10.0.0.0/8"


def test_cidr_open_all_choice_offered_and_selectable(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "detect_public_ip", lambda *a, **k: None)
    monkeypatch.delenv("TG_REQUIRE_IP_ALLOWLIST", raising=False)
    r = _ChoicePicker("Open to all", interactive=True,
                      scripted=_scripted_no_cidrs())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["ingress_cidrs"] == "0.0.0.0/0"


def test_cidr_open_all_not_offered_when_login_off(monkeypatch):
    # login off + no detection → only custom remains, so no menu shows
    # (a one-item pick-list is skipped); the manual question runs and is
    # answered from the scripted ingress_cidrs.
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "detect_public_ip", lambda *a, **k: None)
    monkeypatch.delenv("TG_REQUIRE_IP_ALLOWLIST", raising=False)
    r = _ChoicePicker("never", manual_cidr="10.0.0.0/8", interactive=True,
                      scripted=_scripted_no_cidrs())
    answers = run_questions(r, {"account_id": "123456789012",
                                "enable_login": "n"})
    assert r.seen_choices is None  # menu skipped (only custom)
    assert answers["ingress_cidrs"] == "10.0.0.0/8"


def test_cidr_open_all_not_offered_when_strict_policy(monkeypatch):
    # strict policy on → open-all is not a choice, but detection still
    # offers detected + custom, so the menu shows. Pick detected.
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "detect_public_ip", lambda *a, **k: "203.0.113.7")
    monkeypatch.setenv("TG_REQUIRE_IP_ALLOWLIST", "1")
    r = _ChoicePicker("Detected", interactive=True,
                      scripted=_scripted_no_cidrs())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert r.seen_choices is not None
    assert not any("Open to all" in c for c in r.seen_choices)
    assert answers["ingress_cidrs"] == "203.0.113.7/32"


def test_detect_public_ip_returns_none_on_failure(monkeypatch):
    import tg_cli.runner as runner
    import urllib.request

    def boom(*a, **k):
        raise OSError("no egress")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert runner.detect_public_ip() is None


# ──────────────── #877: prebuilt public-ECR image default ───────────

def _scripted_no_image(**overrides):
    s = _scripted(**overrides)
    s.pop("image", None)
    return s


class _ImagePicker(Resolver):
    """Answers the image_choice menu with a substring match; answers the
    manual `image` URI fallback with `manual_uri`. Records the offered
    choices in .seen_choices."""
    def __init__(self, want, manual_uri=None, **kw):
        super().__init__(**kw)
        self._want = want
        self._manual = manual_uri
        self.seen_choices = None

    def ask(self, q):
        if q.key == "image_choice":
            self.seen_choices = list(q.choices)
            for c in q.choices:
                if self._want in c:
                    return c
            raise AssertionError(f"no choice matched {self._want!r}: {q.choices}")
        if q.key == "image" and self._manual is not None:
            return self._checked(q, self._manual)
        return super().ask(q)


_PREBUILT = "public.ecr.aws/e9y1g4o2/tg-container:v1.2.3"


def test_image_prebuilt_is_default_when_resolvable(monkeypatch):
    """#877: a resolvable prebuilt image is offered as the recommended
    default and selecting it maps to the public ECR URI (no Docker)."""
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "resolve_prebuilt_image", lambda *a, **k: _PREBUILT)
    r = _ImagePicker("prebuilt", interactive=True, scripted=_scripted_no_image())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["image"] == _PREBUILT
    env = to_env(answers)
    assert env["TG_ECS_IMAGE_URI"] == _PREBUILT


def test_image_build_choice_maps_to_build(monkeypatch):
    """#877: choosing build keeps today's behavior (image='build', no
    TG_ECS_IMAGE_URI emitted)."""
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "resolve_prebuilt_image", lambda *a, **k: _PREBUILT)
    r = _ImagePicker("Build from source", interactive=True,
                     scripted=_scripted_no_image())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["image"] == "build"
    assert "TG_ECS_IMAGE_URI" not in to_env(answers)


def test_image_custom_uri_choice(monkeypatch):
    """#877: the advanced custom-URI path still works."""
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "resolve_prebuilt_image", lambda *a, **k: _PREBUILT)
    custom = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mine:tag"
    r = _ImagePicker("Advanced", manual_uri=custom, interactive=True,
                     scripted=_scripted_no_image())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["image"] == custom
    assert to_env(answers)["TG_ECS_IMAGE_URI"] == custom


def test_image_offline_falls_back_to_build(monkeypatch):
    """#877: no resolvable prebuilt image → the prebuilt option is NOT
    offered and the wizard never suggests an unpullable ref. Choosing
    build (the now-default) works."""
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "resolve_prebuilt_image", lambda *a, **k: None)
    r = _ImagePicker("Build from source", interactive=True,
                     scripted=_scripted_no_image())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert r.seen_choices is not None
    assert not any("prebuilt" in c.lower() for c in r.seen_choices)
    assert answers["image"] == "build"


def test_image_presupplied_skips_menu(monkeypatch):
    """#877: a pre-supplied image (env/config/scripted) flows through the
    manual question unchanged — no menu, byte-identical to pre-#877."""
    import tg_cli.runner as runner
    called = {"n": 0}
    monkeypatch.setattr(
        runner, "resolve_prebuilt_image",
        lambda *a, **k: (called.__setitem__("n", called["n"] + 1) or _PREBUILT))
    r = _ImagePicker("never", interactive=True, scripted=_scripted(image="build"))
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["image"] == "build"
    assert r.seen_choices is None          # menu never shown
    assert called["n"] == 0                # probe not even attempted


_UPGRADE_LATEST = "public.ecr.aws/e9y1g4o2/tg-container:latest"
_PRIVATE_DIGEST = (
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
    "tg-container@sha256:9c51fa00deadbeef")


def test_upgrade_defaults_to_public_latest_not_deployed(monkeypatch):
    """#1059: an upgrade re-run whose deployed image is a private digest
    defaults to the public `latest`, NOT the stale/private ref, and the
    detected image is offered only in the Advanced prefill."""
    import tg_cli.wizard as wizard
    import tg_cli.runner as runner
    monkeypatch.setattr(
        runner, "resolve_upgrade_image", lambda *a, **k: _UPGRADE_LATEST)
    monkeypatch.setattr(
        runner, "upgrade_behind_notice", lambda *a, **k: None)
    r = _ImagePicker("prebuilt", interactive=True)
    answers = wizard._ask_image(r, {
        "_is_upgrade": True, "_image_from": _PRIVATE_DIGEST})
    # default-selected prebuilt is the PUBLIC latest, not the digest.
    assert answers["image"] == _UPGRADE_LATEST
    assert _PRIVATE_DIGEST not in (r.seen_choices or [])


def test_upgrade_keeps_current_via_advanced_prefill(monkeypatch):
    """#1059: the detected deployed image pre-fills the Advanced path so
    'keep what I have' is one keystroke — but it's not the top default."""
    import tg_cli.wizard as wizard
    import tg_cli.runner as runner
    monkeypatch.setattr(
        runner, "resolve_upgrade_image", lambda *a, **k: _UPGRADE_LATEST)
    monkeypatch.setattr(
        runner, "upgrade_behind_notice", lambda *a, **k: None)
    seen = {}

    class _AdvPicker(_ImagePicker):
        def ask(self, q):
            if q.key == "image" and q.default is not None:
                seen["default"] = q.default
                return self._checked(q, q.default)  # one-keystroke keep
            return super().ask(q)

    r = _AdvPicker("Advanced", interactive=True)
    answers = wizard._ask_image(r, {
        "_is_upgrade": True, "_image_from": _PRIVATE_DIGEST})
    # Advanced prefill = the deployed image; Enter keeps it on purpose.
    assert seen["default"] == _PRIVATE_DIGEST
    assert answers["image"] == _PRIVATE_DIGEST


def test_upgrade_noninteractive_no_image_defaults_latest(monkeypatch):
    """#1059: a non-interactive upgrade with NO operator-supplied image
    defaults to public latest (never the deployed digest, never a silent
    'build')."""
    import tg_cli.wizard as wizard
    import tg_cli.runner as runner
    monkeypatch.setattr(
        runner, "resolve_upgrade_image", lambda *a, **k: _UPGRADE_LATEST)
    from tg_cli.prompts import Resolver
    r = Resolver(interactive=False)
    answers = wizard._ask_image(r, {
        "_is_upgrade": True, "_image_from": _PRIVATE_DIGEST})
    assert answers["image"] == _UPGRADE_LATEST


def test_upgrade_noninteractive_explicit_image_honored(monkeypatch):
    """#1059: an explicitly-supplied image on a non-interactive upgrade
    is honored byte-for-byte (no override to latest)."""
    import tg_cli.wizard as wizard
    import tg_cli.runner as runner
    monkeypatch.setattr(
        runner, "resolve_upgrade_image", lambda *a, **k: _UPGRADE_LATEST)
    from tg_cli.prompts import Resolver
    explicit = "public.ecr.aws/e9y1g4o2/tg-container:v0.9"
    r = Resolver(interactive=False, supplied={"image": explicit})
    answers = wizard._ask_image(r, {
        "_is_upgrade": True, "_image_from": _PRIVATE_DIGEST})
    assert answers["image"] == explicit


def test_is_unpullable_default(monkeypatch):
    """#1059: private-ECR host or digest-pin ⇒ never a silent default."""
    import tg_cli.runner as runner
    assert runner.is_unpullable_default(_PRIVATE_DIGEST) is True
    assert runner.is_unpullable_default(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/x:tag") is True
    assert runner.is_unpullable_default(
        "public.ecr.aws/e9y1g4o2/tg-container@sha256:abc") is True
    assert runner.is_unpullable_default(_UPGRADE_LATEST) is False
    assert runner.is_unpullable_default(None) is False


def test_resolve_upgrade_image_is_channel_latest(monkeypatch):
    """#1059: upgrade default resolves to the channel `latest` when
    pullable, else None (→ build). Distinct from the greenfield
    version-pin."""
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "public_image_available",
                        lambda tag, **k: tag == "latest")
    assert runner.resolve_upgrade_image() == runner.public_image_ref("latest")
    monkeypatch.setattr(runner, "public_image_available", lambda tag, **k: False)
    assert runner.resolve_upgrade_image() is None


def test_upgrade_behind_notice_honest(monkeypatch):
    """#1059: behind-notice fires only when order is KNOWN (public tags,
    digests differ); soft copy for a private/digest deployed image; silent
    when the order can't be determined."""
    import tg_cli.runner as runner
    latest = runner.public_image_ref("latest")
    old = runner.public_image_ref("stage-20260101-0000")

    # (1) public deployed tag, digests differ → confident 'out of date'.
    monkeypatch.setattr(
        runner, "public_image_digest",
        lambda tag, **k: "sha256:OLD" if "stage-" in tag else "sha256:NEW")
    n = runner.upgrade_behind_notice(old, latest)
    assert n and "out of date" in n.lower()

    # equal digests → already current → no notice.
    monkeypatch.setattr(
        runner, "public_image_digest", lambda tag, **k: "sha256:SAME")
    assert runner.upgrade_behind_notice(old, latest) is None

    # (2) private/digest deployed image → soft 'pinned' copy.
    n2 = runner.upgrade_behind_notice(_PRIVATE_DIGEST, latest)
    assert n2 and "pinned" in n2.lower()

    # (3) can't tell (digest probe returns None) → silent.
    monkeypatch.setattr(
        runner, "public_image_digest", lambda tag, **k: None)
    assert runner.upgrade_behind_notice(old, latest) is None


def test_resolve_prebuilt_image_prefers_version_then_channel(monkeypatch):
    """#877: resolve prefers the checkout version; falls back to the
    channel tag; returns None when neither is pullable."""
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_version", lambda: "v9.9.9")
    # version pullable → version ref
    monkeypatch.setattr(runner, "public_image_available",
                        lambda tag, **k: tag == "v9.9.9")
    assert runner.resolve_prebuilt_image() == runner.public_image_ref("v9.9.9")
    # version NOT pullable, channel is → channel ref
    monkeypatch.setattr(runner, "public_image_available",
                        lambda tag, **k: tag == "latest")
    assert runner.resolve_prebuilt_image() == runner.public_image_ref("latest")
    # neither pullable → None (wizard falls back to build)
    monkeypatch.setattr(runner, "public_image_available", lambda tag, **k: False)
    assert runner.resolve_prebuilt_image() is None


def test_public_image_available_false_on_failure(monkeypatch):
    """#877: any network failure → not pullable (never suggest it)."""
    import tg_cli.runner as runner
    import urllib.request

    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert runner.public_image_available("v1.2.3") is False


def test_account_and_email_and_arn_validators():
    assert V.account_id("123456789012") is None
    assert V.account_id("12345") is not None
    assert V.email("a@b.co") is None
    assert V.email("nope") is not None
    assert V.cert_arn("arn:aws:acm:us-east-1:123456789012:certificate/x") is None
    assert V.cert_arn("arn:bogus") is not None


def test_cert_arn_validator_is_shape_only_not_existence():
    """#888: cert_arn is a SHAPE gate only — a well-formed but
    nonexistent/placeholder ARN (the `dummy` case) still passes the
    validator; existence is checked installer-side with creds. This
    pins the contract so the validator isn't mistaken for a liveness
    check."""
    # the literal `dummy` resource id is shape-valid → passes here
    assert V.cert_arn(
        "arn:aws:acm:us-east-1:123456789012:certificate/dummy") is None
    # gov/partition ARNs still accepted by shape
    assert V.cert_arn(
        "arn:aws-us-gov:acm:us-gov-west-1:123456789012:certificate/x"
    ) is None
    # genuinely malformed → rejected
    assert V.cert_arn("not-an-arn") is not None
    assert V.cert_arn("arn:aws:iam::123456789012:role/x") is not None


def test_cert_existing_why_is_self_service():
    """#888: the existing-ARN WHY explains what ACM is, where to find
    an ARN (console + CLI), and the self-signed / auto-issue
    alternatives — self-service for someone who doesn't know ACM."""
    asked = {}

    class Capture(Resolver):
        def ask(self, q):
            if q.key == "cert_arn":
                asked["why"] = q.why
                return "arn:aws:acm:us-east-1:123456789012:certificate/x"
            return super().ask(q)

    r = Capture(interactive=True, scripted=_scripted(cert_mode=CERT_EXISTING))
    run_questions(r, {"account_id": "123456789012"})
    why = asked.get("why", "")
    assert "ACM" in why and "Certificate Manager" in why
    assert "list-certificates" in why          # the CLI discovery path
    assert "self-signed" in why                # the no-cert alternative
    assert "TG_ISSUE_ACM_CERT" in why          # the auto-issue alternative


# ────────────────────── L1: 7-Q flow + cert 3-way ───────────────────

def _scripted(**overrides):
    base = {
        "region": "us-east-1",
        "ingress_cidrs": "203.0.113.0/24",
        "image": "build",
        "iam_prefix": "tg-",
        # CUR is no longer a question — it's an informational notice
        # (the sole spend source; the customer has no choice). No
        # enable_cur key is scripted.
        "bootstrap_email": "admin@example.com",
        # #921: blank = Option A (random throwaway + forgot-password),
        # the default headless path. A non-empty override exercises
        # Option B (operator-provided) — see the #921 tests.
        "bootstrap_password": "",
        "cert_mode": CERT_EXISTING,
        "cert_arn": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        # #796: this base scripts a bring-your-own OIDC issuer, so the
        # provider is okta (the path that asks oidc_issuer/client_id).
        "auth_provider": AUTH_OKTA,
        "oidc_issuer": "https://example.okta.com",
        "oidc_client_id": "client-abc",
        # #774: default to create-new VPC (greenfield, no subnet Qs).
        "vpc_mode": VPC_CREATE,
    }
    base.update(overrides)
    return base


def test_full_flow_existing_cert_maps_to_env():
    r = Resolver(interactive=True, scripted=_scripted())
    answers = run_questions(r, {"account_id": "123456789012"})
    env = to_env(answers)
    assert env["AWS_REGION"] == "us-east-1"
    assert env["TG_ALLOWED_INGRESS_CIDRS"] == "203.0.113.0/24"
    assert env["TG_BOOTSTRAP_ADMIN_EMAIL"] == "admin@example.com"
    assert env["TG_CERT_ARN"].startswith("arn:aws:acm:")
    assert "TG_ALLOW_PLAINTEXT_ALB" not in env


def test_cert_plaintext_requires_explicit_confirm():
    # 'no' confirm must bounce back; then a 'yes' second pass proceeds.
    seq = {"calls": 0}

    class Bounce(Resolver):
        def ask(self, q):
            if q.key == "plaintext_confirm":
                seq["calls"] += 1
                return "no" if seq["calls"] == 1 else "yes"
            return super().ask(q)

    r = Bounce(interactive=True, scripted=_scripted(cert_mode=CERT_PLAINTEXT))
    answers = run_questions(r, {"account_id": "123456789012"})
    # bounced once (asked cert_mode + confirm twice), ended on plaintext
    assert seq["calls"] == 2
    env = to_env(answers)
    assert env["TG_ALLOW_PLAINTEXT_ALB"] == "1"
    assert "TG_CERT_ARN" not in env


def test_cert_selfsigned_defers_arn_to_deploy():
    r = Resolver(interactive=True, scripted=_scripted(cert_mode=CERT_SELFSIGNED))
    answers = run_questions(r, {"account_id": "123456789012"})
    env = to_env(answers)
    # self-signed ARN is filled at deploy time by the helper, not here.
    assert "TG_CERT_ARN" not in env
    assert "TG_ALLOW_PLAINTEXT_ALB" not in env


def test_non_interactive_missing_required_aborts():
    # No scripted/supplied value for a required Q with no default → abort.
    r = Resolver(interactive=False, supplied={"region": "us-east-1"})
    with pytest.raises(PromptAbort):
        run_questions(r, {})


# ── #988: ACM cert pick-list in the wizard (mirror #774/#877) ────────
#
# The existing-cert path now LISTS ISSUED ACM certs and offers a
# pick-by-domain menu; a pre-supplied cert_arn / non-interactive run
# still flows through the hand-typed Question and NEVER lists.


def test_list_acm_certs_query_flags():
    """#988: the helper shells out with --certificate-statuses ISSUED
    AND the mandatory --includes keyTypes=... (the default-RSA_2048-only
    gotcha — without it ECDSA/RSA_4096 ALB certs are invisible)."""
    import inspect
    from tg_cli import runner
    src = inspect.getsource(runner.list_acm_certs)
    assert "ISSUED" in src
    assert "keyTypes=" in src
    # all RSA + EC key types listed (not just the RSA_2048 default)
    for kt in ("RSA_4096", "EC_prime256v1", "EC_secp384r1"):
        assert kt in src


def test_cert_picklist_lists_and_maps_to_arn(monkeypatch):
    """Interactive + ≥1 ISSUED cert → a domain-labeled menu; the picked
    label maps back to the full ARN in answers['cert_arn']."""
    from tg_cli import wizard, runner
    arn = "arn:aws:acm:us-east-1:123456789012:certificate/abc-123"
    monkeypatch.setattr(runner, "list_acm_certs", lambda *a, **k: [
        {"arn": arn, "domain": "tg.example.com"},
        {"arn": "arn:aws:acm:us-east-1:123456789012:certificate/other",
         "domain": "old.example.com"},
    ])
    seen = {}

    class _P(Resolver):
        def ask(self, q):
            if q.key == "cert_pick":
                seen["choices"] = list(q.choices)
                # pick the tg.example.com entry by substring
                for c in q.choices:
                    if "tg.example.com" in c:
                        return c
            return super().ask(q)

    r = _P(interactive=True)
    out = wizard._ask_cert_arn(r, {"region": "us-east-1"})
    assert out == arn                                  # label → full ARN
    assert any("tg.example.com" in c for c in seen["choices"])
    assert wizard.CERT_ARN_MANUAL in seen["choices"]   # manual escape offered


def test_cert_picklist_manual_escape_falls_through(monkeypatch):
    """Choosing 'Enter an ARN manually' → the hand-typed cert_arn
    Question (with V.cert_arn validation)."""
    from tg_cli import wizard, runner
    monkeypatch.setattr(runner, "list_acm_certs", lambda *a, **k: [
        {"arn": "arn:aws:acm:us-east-1:123456789012:certificate/x",
         "domain": "tg.example.com"},
    ])
    typed = "arn:aws:acm:us-east-1:123456789012:certificate/typed"

    class _P(Resolver):
        def ask(self, q):
            if q.key == "cert_pick":
                return wizard.CERT_ARN_MANUAL
            if q.key == "cert_arn":
                return typed
            return super().ask(q)

    out = wizard._ask_cert_arn(_P(interactive=True), {"region": "us-east-1"})
    assert out == typed


def test_cert_picklist_empty_falls_through_with_note(monkeypatch):
    """No ISSUED certs (or AWS error → []) → manual Question, no crash,
    a note emitted."""
    from tg_cli import wizard, runner
    monkeypatch.setattr(runner, "list_acm_certs", lambda *a, **k: [])
    notes = []
    typed = "arn:aws:acm:us-east-1:123456789012:certificate/m"

    class _P(Resolver):
        def note(self, m):
            notes.append(m)

        def ask(self, q):
            if q.key == "cert_arn":
                return typed
            return super().ask(q)

    out = wizard._ask_cert_arn(_P(interactive=True), {"region": "us-east-1"})
    assert out == typed
    assert any("No ISSUED ACM cert" in n for n in notes)


def test_cert_presupplied_never_lists(monkeypatch):
    """A pre-supplied cert_arn (env/config) flows through the manual
    Question and NEVER calls list_acm_certs (acceptance: no AWS call)."""
    from tg_cli import wizard, runner
    monkeypatch.setattr(runner, "list_acm_certs",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not list when pre-supplied")))
    arn = "arn:aws:acm:us-east-1:123456789012:certificate/pre"
    r = Resolver(interactive=True, supplied={"cert_arn": arn})
    out = wizard._ask_cert_arn(r, {"region": "us-east-1", "cert_arn": arn})
    assert out == arn


# ── #995: upgrade-seeded cert_arn must NOT skip the pick-list ────────
#
# #962 seeds answers["cert_arn"] from the deployed stack's CertificateArn
# on an upgrade. That's a DEFAULT to pre-select, not an operator supply —
# the #988 guard must still show the menu (it conflated the two, so the
# pick-list never appeared on a re-install).


def test_cert_upgrade_seeded_still_lists_and_preselects(monkeypatch):
    """Interactive upgrade: cert_arn is in `answers` (seeded by #962) but
    NOT in resolver.supplied → the menu STILL shows, with the deployed
    cert pre-selected as the default; Enter keeps it."""
    from tg_cli import wizard, runner
    seeded = "arn:aws:acm:us-east-1:123456789012:certificate/deployed"
    monkeypatch.setattr(runner, "list_acm_certs", lambda *a, **k: [
        {"arn": "arn:aws:acm:us-east-1:123456789012:certificate/other",
         "domain": "old.example.com"},
        {"arn": seeded, "domain": "tg.example.com"},
    ])
    seen = {}

    class _P(Resolver):
        def ask(self, q):
            if q.key == "cert_pick":
                seen["default"] = q.default
                seen["choices"] = list(q.choices)
                return q.default          # Enter = keep the default
            return super().ask(q)

    # seeded in answers (like #962), NOT in supplied → menu must show
    out = wizard._ask_cert_arn(
        _P(interactive=True), {"region": "us-east-1", "cert_arn": seeded})
    assert seen.get("choices"), "pick-list must appear on an upgrade"
    # the deployed cert is the highlighted default → Enter keeps it
    assert "tg.example.com" in seen["default"]
    assert out == seeded


def test_cert_upgrade_seeded_absent_from_list_defaults_first(monkeypatch):
    """Upgrade + the seeded cert is NOT in the ISSUED list (rotated /
    deleted): menu still shows, first entry is the default, no crash."""
    from tg_cli import wizard, runner
    gone = "arn:aws:acm:us-east-1:123456789012:certificate/rotated-away"
    monkeypatch.setattr(runner, "list_acm_certs", lambda *a, **k: [
        {"arn": "arn:aws:acm:us-east-1:123456789012:certificate/live",
         "domain": "current.example.com"},
    ])
    seen = {}

    class _P(Resolver):
        def ask(self, q):
            if q.key == "cert_pick":
                seen["default"] = q.default
                return q.default
            return super().ask(q)

    out = wizard._ask_cert_arn(
        _P(interactive=True), {"region": "us-east-1", "cert_arn": gone})
    # falls back to the first listed entry (not the absent seeded ARN)
    assert "current.example.com" in seen["default"]
    assert out == "arn:aws:acm:us-east-1:123456789012:certificate/live"


# ── #999: resolver.supplied must NOT alias the answers working dict ──
#
# The live defeat of #995: __main__ built `Resolver(supplied=answers)`
# — the SAME dict — so the #962 upgrade-seed into answers["cert_arn"]
# also showed up in resolver.supplied, tripping _ask_cert_arn's
# supplied-skip guard. #995's own tests passed a SEPARATE supplied dict,
# so they never reproduced the alias. These build the resolver the way
# __main__ does (a dict() snapshot) and assert the separation holds.


def test_resolver_supplied_snapshot_not_aliased_by_seed():
    """#999: a dict() snapshot at construction means a LATER write to the
    answers working dict (the #962 deployed-default seed) does NOT leak
    into resolver.supplied — the two are genuinely separable."""
    answers = {"region": "us-east-1"}          # env/config so far
    r = Resolver(interactive=True, supplied=dict(answers))  # __main__'s pattern
    # #962 seeds the deployed cert into the WORKING dict afterwards:
    answers["cert_arn"] = "arn:aws:acm:us-east-1:123456789012:certificate/seed"
    # the seed must NOT have leaked into the frozen supply snapshot
    assert "cert_arn" not in r.supplied


def test_cert_upgrade_menu_shows_with_mainstyle_resolver(monkeypatch):
    """#999 regression (reproduces the live aliasing #995's tests missed):
    build the resolver like __main__ — supplied = dict(answers) snapshot —
    then seed answers["cert_arn"] (the #962 upgrade default). The pick-list
    MUST still appear with the seeded cert pre-selected; it must NOT be
    treated as a supplied value and skipped."""
    from tg_cli import wizard, runner
    seeded = "arn:aws:acm:us-east-1:123456789012:certificate/deployed"
    monkeypatch.setattr(runner, "list_acm_certs", lambda *a, **k: [
        {"arn": seeded, "domain": "tg.example.com"},
    ])
    seen = {}

    class _P(Resolver):
        def ask(self, q):
            if q.key == "cert_pick":
                seen["shown"] = True
                return q.default
            return super().ask(q)

    answers = {"region": "us-east-1"}
    # __main__'s construction: supply snapshot taken BEFORE the seed.
    r = _P(interactive=True, supplied=dict(answers))
    answers["cert_arn"] = seeded               # #962 seeds the working dict
    out = wizard._ask_cert_arn(r, answers)
    assert seen.get("shown"), "menu must appear — seed must not skip it"
    assert out == seeded


def test_cert_genuinely_supplied_still_skips_mainstyle(monkeypatch):
    """#999: a GENUINELY env/CLI-supplied cert_arn (present in the supply
    snapshot at construction) STILL skips the menu — byte-identical to
    pre-#988. The fix must not over-correct and start listing on a real
    supply."""
    from tg_cli import wizard, runner
    monkeypatch.setattr(runner, "list_acm_certs",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not list on a real supply")))
    arn = "arn:aws:acm:us-east-1:123456789012:certificate/env"
    answers = {"region": "us-east-1", "cert_arn": arn}   # env-supplied
    r = Resolver(interactive=True, supplied=dict(answers))  # snapshot HAS it
    out = wizard._ask_cert_arn(r, answers)
    assert out == arn


# ── #1000: build version at install start + deployed-version match ──


def test_build_version_is_version_file_scheme(monkeypatch, tmp_path):
    """#1088: runner.build_version() derives v<VERSION>-g<sha> from the
    committed VERSION file + short HEAD SHA — NOT a git describe tag (a
    force-moved release tag a naive `git pull` leaves stale makes
    describe fall through to a bare SHA on customer clones). Mocks the
    git calls + the VERSION read; no real git."""
    from tg_cli import runner

    class _P:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if "rev-parse" in cmd:
            return _P(0, "42879ea\n")
        if "status" in cmd:
            return _P(0, "")   # clean tree
        return _P(0, "")
    (tmp_path / "VERSION").write_text("1.1.0\n")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.build_version() == "v1.1.0-g42879ea"
    # the banner no longer git-describes.
    assert not any(c[:2] == ["git", "describe"] for c in calls)


def test_build_version_falls_back_to_dev(monkeypatch, tmp_path):
    """No VERSION file AND all git probes fail → 'dev'."""
    from tg_cli import runner

    def fake_run(cmd, **k):
        raise OSError("no git")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)  # no VERSION here
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.build_version() == "dev"


def test_version_single_sources_from_version_file():
    """tg_cli.__version__ reads the tracked VERSION file at repo root —
    the same single source the publish flow tags the public orphan with
    — so `tg --version` and the build banner can't drift. Asserts the
    two are byte-identical (the PyPA single-source pattern)."""
    import re

    import tg_cli
    version_file = _REPO / "VERSION"
    assert version_file.is_file(), "VERSION file must exist at repo root"
    want = version_file.read_text().strip()
    assert want, "VERSION file must not be empty"
    # bare semver, no `v` prefix (the publish flow adds the `v`).
    assert re.match(r"^\d+\.\d+\.\d+$", want), \
        f"VERSION must be a bare semver, got {want!r}"
    assert tg_cli.__version__ == want
    assert tg_cli.__version__ != "0.1.0"   # the old hardcoded drift


def test_deployed_version_parses_api_version(monkeypatch):
    """deployed_version reads {version} from <url>/api/version; None on
    any error (app not reachable). urllib is imported inside the fn, so
    patch the real urllib.request.urlopen."""
    from tg_cli import runner
    import io
    import urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda url, **k: _Resp(b'{"version": "stage-20260613-0327-6-g42879ea"}'))
    v = runner.deployed_version("https://tg-alb-123.elb.amazonaws.com")
    assert v == "stage-20260613-0327-6-g42879ea"


def test_deployed_version_none_on_unreachable(monkeypatch):
    from tg_cli import runner
    import urllib.request

    def boom(url, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert runner.deployed_version("https://nope.example.com") is None


def test_version_match_prints_match(monkeypatch, capsys):
    """#1000: deployed == build → the explicit ✓ match line."""
    import tg_cli.__main__ as m
    monkeypatch.setattr(m.runner, "stack_output",
                        lambda *a, **k: "tg-alb-123.elb.amazonaws.com")
    monkeypatch.setattr(m.runner, "deployed_version", lambda *a, **k: "v9")
    m._print_version_match({"region": "us-east-1"}, "v9")
    out = capsys.readouterr().out
    assert "matches this build (v9)" in out


def test_version_match_prints_mismatch(monkeypatch, capsys):
    """#1000: deployed != build → a warning naming both versions."""
    import tg_cli.__main__ as m
    monkeypatch.setattr(m.runner, "stack_output",
                        lambda *a, **k: "tg-alb-123.elb.amazonaws.com")
    monkeypatch.setattr(m.runner, "deployed_version", lambda *a, **k: "v8")
    m._print_version_match({"region": "us-east-1"}, "v9")
    out = capsys.readouterr().out
    assert "v8" in out and "v9" in out and "cached image" in out


def test_version_match_quiet_when_unreachable(monkeypatch, capsys):
    """#1000: app not reachable yet → print nothing (never fail/ noise
    the install on a best-effort probe)."""
    import tg_cli.__main__ as m
    monkeypatch.setattr(m.runner, "stack_output",
                        lambda *a, **k: "tg-alb-123.elb.amazonaws.com")
    monkeypatch.setattr(m.runner, "deployed_version", lambda *a, **k: None)
    m._print_version_match({"region": "us-east-1"}, "v9")
    assert capsys.readouterr().out == ""


# ──────────────── L1: config resume / idempotency / no secrets ──────

def test_config_roundtrip_and_secret_scrub(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_CONFIG_HOME", str(tmp_path))
    import importlib
    importlib.reload(config)
    config.save({
        "region": "us-east-1",
        "bootstrap_email": "a@b.co",
        "oidc_client_secret": "SHOULD-NOT-PERSIST",
        "TG_OIDC_CLIENT_SECRET": "ALSO-NOT",
    })
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["region"] == "us-east-1"
    assert "oidc_client_secret" not in on_disk
    assert "TG_OIDC_CLIENT_SECRET" not in on_disk
    # resume: load returns what we saved (idempotent re-save is stable)
    assert config.load()["bootstrap_email"] == "a@b.co"
    config.save(config.load())
    assert json.loads((tmp_path / "config.json").read_text()) == on_disk
    # cleanup + reload module so other tests see the real home
    monkeypatch.delenv("TG_CONFIG_HOME", raising=False)
    importlib.reload(config)


# ───────── L1: #874 account-keyed config (multi-install safety) ──────

def test_account_keyed_config_path_isolation(tmp_path, monkeypatch):
    """Two installs targeting different accounts write distinct files;
    neither resumes the other's answers."""
    monkeypatch.setenv("TG_CONFIG_HOME", str(tmp_path))
    import importlib
    importlib.reload(config)
    config.save({"account_id": "123456789012", "region": "us-east-1"},
                "123456789012")
    config.save({"account_id": "123456789012", "region": "eu-west-2"},
                "123456789012")
    # distinct files on disk
    assert (tmp_path / "config-123456789012.json").exists()
    assert (tmp_path / "config-123456789012.json").exists()
    # each loads only its own answers — no cross-resume
    assert config.load("123456789012")["region"] == "us-east-1"
    assert config.load("123456789012")["region"] == "eu-west-2"
    monkeypatch.delenv("TG_CONFIG_HOME", raising=False)
    importlib.reload(config)


def test_migrate_legacy_adopts_matching_account(tmp_path, monkeypatch):
    """A legacy config.json whose account matches is adopted under the
    account-keyed name (single-install user keeps resuming)."""
    monkeypatch.setenv("TG_CONFIG_HOME", str(tmp_path))
    import importlib
    importlib.reload(config)
    # legacy neutral file for account A
    config.save({"account_id": "123456789012", "bootstrap_email": "a@b.co"})
    adopted = config.migrate_legacy("123456789012")
    assert adopted["bootstrap_email"] == "a@b.co"
    assert (tmp_path / "config-123456789012.json").exists()
    monkeypatch.delenv("TG_CONFIG_HOME", raising=False)
    importlib.reload(config)


def test_migrate_legacy_refuses_mismatched_account(tmp_path, monkeypatch):
    """A legacy config.json for a DIFFERENT account is NOT adopted —
    the silent cross-contamination bug #874 fixes."""
    monkeypatch.setenv("TG_CONFIG_HOME", str(tmp_path))
    import importlib
    importlib.reload(config)
    # legacy file belongs to account A
    config.save({"account_id": "123456789012", "region": "us-east-1"})
    # installing into account B must NOT inherit A's answers
    adopted = config.migrate_legacy("123456789012")
    assert adopted == {}
    assert not (tmp_path / "config-123456789012.json").exists()
    monkeypatch.delenv("TG_CONFIG_HOME", raising=False)
    importlib.reload(config)


def test_migrate_legacy_adopts_accountless_config(tmp_path, monkeypatch):
    """A pre-#874 legacy config.json with no account_id recorded is
    adopted for whatever account is now resolved (the single-install
    upgrade path)."""
    monkeypatch.setenv("TG_CONFIG_HOME", str(tmp_path))
    import importlib
    importlib.reload(config)
    config.save({"region": "us-east-1"})  # no account_id
    adopted = config.migrate_legacy("123456789012")
    assert adopted["region"] == "us-east-1"
    monkeypatch.delenv("TG_CONFIG_HOME", raising=False)
    importlib.reload(config)


def test_caller_account_suggestion(monkeypatch):
    """#874: runner.caller_account parses a 12-digit Account from
    `aws sts get-caller-identity`; bad/short output → None."""
    from tg_cli import runner
    import subprocess as _sp

    class _R:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    # valid 12-digit account
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R(0, "123456789012\n"))
    assert runner.caller_account() == "123456789012"
    # non-numeric / error → None
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R(0, "not-an-acct\n"))
    assert runner.caller_account() is None
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R(255, ""))
    assert runner.caller_account() is None


# ───────── #1027: caller != target account, surfaced early ─────────
# _enforce_account_match runs right after the account question, BEFORE
# the rest of the wizard. A scripted/supplied resolver lets us prove the
# control flow without a TTY or real STS.

def _enforce():
    from tg_cli.__main__ import _enforce_account_match
    return _enforce_account_match


class _NotingResolver(Resolver):
    """Records every note() so a test can assert the operator was told."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.notes: list[str] = []

    def note(self, msg: str) -> None:  # capture instead of printing
        self.notes.append(msg)


def test_account_match_mismatch_noninteractive_hard_fails():
    """#1027: --non-interactive can't reprompt → PromptAbort fast, naming
    both accounts, BEFORE the rest of the wizard / installer launch."""
    r = _NotingResolver(interactive=False)
    answers = {"account_id": "123456789012"}
    with pytest.raises(PromptAbort) as ei:
        _enforce()(r, answers, "123456789012")
    msg = str(ei.value)
    assert "123456789012" in msg and "123456789012" in msg
    assert "AWS_PROFILE" in msg


def test_account_match_mismatch_interactive_reprompts_then_corrects():
    """#1027: interactive mismatch → note() the fix + re-ask; once the
    operator enters the matching account, it proceeds with that value."""
    r = _NotingResolver(interactive=True,
                        scripted={"account_id": "123456789012"})
    answers = {"account_id": "123456789012"}
    _enforce()(r, answers, "123456789012")
    # corrected to the caller account; the operator was told why.
    assert answers["account_id"] == "123456789012"
    assert r.notes and "123456789012" in r.notes[0] \
        and "123456789012" in r.notes[0]
    assert "AWS_PROFILE" in r.notes[0]


def test_account_match_cross_account_confirmed_by_reentry():
    """#1027: re-entering the SAME (mismatched) target confirms a
    deliberate cross-account deploy → proceeds, doesn't loop forever."""
    r = _NotingResolver(interactive=True,
                        scripted={"account_id": "123456789012"})
    answers = {"account_id": "123456789012"}
    _enforce()(r, answers, "123456789012")
    # kept the operator's deliberate cross-account target; warned once.
    assert answers["account_id"] == "123456789012"
    assert len(r.notes) == 1  # warned exactly once, then accepted


def test_account_match_caller_unresolvable_is_skipped():
    """#1027: caller account None (no creds / STS fail) → no comparison,
    no crash, no prompt. The installer preflight remains the backstop."""
    r = _NotingResolver(interactive=False)
    answers = {"account_id": "123456789012"}
    _enforce()(r, answers, None)  # must not raise
    assert answers["account_id"] == "123456789012"
    assert r.notes == []


def test_account_match_equal_is_noop():
    """#1027: caller == target → no note, no reprompt, value unchanged."""
    r = _NotingResolver(interactive=True,
                        scripted={"account_id": "SHOULD_NOT_BE_ASKED"})
    answers = {"account_id": "123456789012"}
    _enforce()(r, answers, "123456789012")
    assert answers["account_id"] == "123456789012"
    assert r.notes == []


# ─────────────────── L2: real-CLI --dry-run smoke ───────────────────

def _run_tg(args, env_extra, config_home):
    env = os.environ.copy()
    env["TG_CONFIG_HOME"] = str(config_home)
    env.update(env_extra)
    return subprocess.run(
        [str(TG), *args], capture_output=True, text=True, env=env, timeout=60
    )


def test_real_cli_help_and_version(tmp_path):
    r = _run_tg(["--version"], {}, tmp_path)
    assert r.returncode == 0 and "tg" in r.stdout


def test_real_cli_install_dry_run_stops_before_mutating(tmp_path):
    """The actual binary validates + prints the plan + exits 0, no AWS."""
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "203.0.113.0/24",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_OIDC_ISSUER": "https://example.okta.com",
        "TG_OIDC_CLIENT_ID": "client-abc",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"], env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "confirm" in r.stdout.lower()
    assert "no resources created" in r.stdout.lower()
    # the env it WOULD export is shown and correct
    assert "TG_ALLOWED_INGRESS_CIDRS=203.0.113.0/24" in r.stdout
    # secret never written to the resume file
    cfg = tmp_path / "config.json"
    if cfg.exists():
        assert "SECRET" not in cfg.read_text().upper() or \
            "CLIENT_SECRET" not in cfg.read_text()


def test_real_cli_install_dry_run_rejects_bad_cidr(tmp_path):
    # #875: 0.0.0.0/0 is rejected when the strict-allowlist policy is on
    # (the Amazon/internal posture). Real-CLI level so the env contract is
    # exercised, not just the validator import.
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "0.0.0.0/0",   # world-open
        "TG_REQUIRE_IP_ALLOWLIST": "1",            # strict → reject
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_OIDC_ISSUER": "https://example.okta.com",
        "TG_OIDC_CLIENT_ID": "client-abc",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"], env_extra, tmp_path)
    assert r.returncode != 0
    assert "0.0.0.0/0" in (r.stderr + r.stdout)


def test_real_cli_install_dry_run_allows_open_all_login_on(tmp_path):
    # #875: with the policy off (default) and login on, 0.0.0.0/0 is a
    # permitted login-gated opt-in — the dry-run plans it without error.
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "0.0.0.0/0",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_OIDC_ISSUER": "https://example.okta.com",
        "TG_OIDC_CLIENT_ID": "client-abc",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"], env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "TG_ALLOWED_INGRESS_CIDRS=0.0.0.0/0" in r.stdout


def test_real_cli_destroy_dry_run_no_mutation(tmp_path):
    r = _run_tg(["destroy", "--dry-run"], {"AWS_REGION": "us-east-1"}, tmp_path)
    assert r.returncode == 0
    assert "nothing deleted" in r.stdout.lower()


# ──────────── L1: Cognito-only install, single-phase (#926) ─────────
# #926 made `tg install` Cognito-only: no auth-provider question, no
# bring-your-own-OIDC prompts, and no Okta two-phase bootstrap. SAML is
# turned on AFTER install via the tg_owns_directory DB flag. The
# scripted base still sets auth_provider=AUTH_OKTA/oidc_issuer (env
# pre-seeds), but the installer now PINS Cognito regardless.

def test_install_is_cognito_only_single_phase():
    """The wizard pins Cognito and clears the OIDC trio even when the
    scripted base pre-seeds an okta issuer — install never federates."""
    r = Resolver(interactive=True, scripted=_scripted())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["auth_provider"] == AUTH_COGNITO
    assert answers["oidc_issuer"] == ""
    assert answers["oidc_client_id"] == ""
    e2 = to_env(answers, phase=2)
    assert e2["TG_AUTH_REQUIRE_LOGIN"] == "1"
    assert e2["TG_AUTH_PROVIDER"] == "cognito"
    # the installer derives OIDC from the pool — never seeded here
    assert "TG_OIDC_ISSUER" not in e2 or e2.get("TG_OIDC_ISSUER") == ""


def test_install_no_auth_provider_or_oidc_questions_asked():
    """#926: NO auth-provider/OIDC question is ever asked — install is
    Cognito-only. (Replaces the pre-#926 'provider question is asked'
    assertion.)"""
    asked = []

    class Track(Resolver):
        def ask(self, q):
            asked.append(q.key)
            return super().ask(q)

    r = Track(interactive=True, scripted=_scripted())
    run_questions(r, {"account_id": "123456789012"})
    assert "auth_provider" not in asked
    assert "oidc_issuer" not in asked
    assert "oidc_client_id" not in asked
    # login is never a question either (always on)
    assert "enable_login" not in asked


def test_login_disabled_is_single_phase():
    # Login is no longer a wizard question — the ONLY route to a
    # login-off install is the TG_AUTH_REQUIRE_LOGIN=0 env backfill,
    # which pre-seeds enable_login="n" in the answers dict (the
    # dev/test escape hatch). Simulate that by seeding it directly.
    r = Resolver(interactive=True, scripted=_scripted())
    answers = run_questions(r, {"account_id": "123456789012",
                                "enable_login": "n"})
    assert to_env(answers, phase=2)["TG_AUTH_REQUIRE_LOGIN"] == "0"


def test_cur_is_not_a_question():
    """F4: no CUR y/n question; enable_cur never enters the answer
    set, and to_env maps it to no env var (it never did)."""
    asked = []

    class Track(Resolver):
        def ask(self, q):
            asked.append(q.key)
            return super().ask(q)

    r = Track(interactive=True, scripted=_scripted())
    answers = run_questions(r, {"account_id": "123456789012"})
    assert "enable_cur" not in asked
    assert "enable_cur" not in answers
    env = to_env(answers)
    assert not any("CUR" in k for k in env)


def test_subnet_empty_submit_notes_spacebar():
    """F1: an empty subnet pick re-asks with a Spacebar-naming note,
    not the bare WHY re-print. Drive _ask_subnets empty -> valid via
    a multi-select resolver that returns [] once, then 2 subnets."""
    from tg_cli import wizard, runner as tg_runner

    notes = []
    seq = {"n": 0}

    class SubnetResolver(Resolver):
        def note(self, msg):
            notes.append(msg)

        def ask_multi(self, q):
            seq["n"] += 1
            # 1st call: empty (the footgun); 2nd: two subnets in 2 AZs.
            # #959: labels now carry the route-egress class, not the
            # MapPublicIpOnLaunch 'public'/'private' string.
            if seq["n"] == 1:
                return []
            return [
                "subnet-0aaa1111  us-east-1a  10.0.1.0/24  public(IGW)",
                "subnet-0bbb2222  us-east-1b  10.0.2.0/24  public(IGW)",
            ]

    # Stub the live describe so the multiselect path is taken offline.
    # #959: both subnets share egress=public → homogeneous, so the
    # egress guard passes and the pair returns (this test exercises the
    # AZ/empty-submit guard, not the egress one).
    orig = tg_runner.list_subnets
    tg_runner.list_subnets = lambda *a, **k: [
        {"id": "subnet-0aaa1111", "az": "us-east-1a",
         "cidr": "10.0.1.0/24", "public": True, "egress": "public"},
        {"id": "subnet-0bbb2222", "az": "us-east-1b",
         "cidr": "10.0.2.0/24", "public": True, "egress": "public"},
    ]
    try:
        r = SubnetResolver(interactive=True)
        out = wizard._ask_subnets(r, {}, "vpc-0abc1234", "us-east-1", None)
    finally:
        tg_runner.list_subnets = orig
    assert out == "subnet-0aaa1111,subnet-0bbb2222"
    assert notes, "expected a re-ask note on the empty submit"
    assert "Spacebar" in notes[0]
    assert "0 subnet" in notes[0] or "0 subnet(s)" in notes[0]


# ── #959: egress validated in the wizard, not just at deploy ─────────
#
# The deploy preflight (#779) requires all BYO subnets to share ONE
# egress type (a Fargate task's AssignPublicIp is one value for every
# ENI). That was enforced only at deploy — AFTER the ACM cert — so a
# mixed set the wizard accepted hard-failed three steps later. These
# move the check to the prompt: label by ROUTE egress (not
# MapPublicIpOnLaunch) + reject a mixed/private-no-endpoint set + re-ask.


def _egress_subnet_resolver(picks_seq, notes_sink):
    """A multiselect Resolver that returns label lists from picks_seq
    (one per ask_multi call) and records notes."""
    seq = {"n": 0}

    class _R(Resolver):
        def note(self, msg):
            notes_sink.append(msg)

        def ask_multi(self, q):
            i = seq["n"]
            seq["n"] += 1
            return picks_seq[min(i, len(picks_seq) - 1)]

    return _R(interactive=True)


def test_subnet_pick_labels_by_route_egress(monkeypatch):
    """#959: the pick-list label is the route-egress class
    (public(IGW)/nat/no-egress), NOT the MapPublicIpOnLaunch flag."""
    from tg_cli import wizard, runner as tg_runner
    monkeypatch.setattr(tg_runner, "list_subnets", lambda *a, **k: [
        # MapPublicIpOnLaunch=True but routes via NAT → label must say nat
        {"id": "subnet-0aaa1111", "az": "us-east-1a",
         "cidr": "10.0.1.0/24", "public": True, "egress": "nat"},
        {"id": "subnet-0bbb2222", "az": "us-east-1b",
         "cidr": "10.0.2.0/24", "public": True, "egress": "nat"},
    ])
    seen = {}

    class _R(Resolver):
        def ask_multi(self, q):
            seen["choices"] = list(q.choices)
            return q.choices[:2]

    out = wizard._ask_subnets(_R(interactive=True), {}, "vpc-0abc1234",
                              "us-east-1", None)
    assert out == "subnet-0aaa1111,subnet-0bbb2222"
    # label reflects egress=nat, never the misleading 'public'
    assert all("nat" in c for c in seen["choices"])
    assert not any(c.endswith("public") for c in seen["choices"])


def test_subnet_mixed_egress_rejected_and_reasked(monkeypatch):
    """#959: a mixed set (1 IGW + 1 NAT) is rejected at the prompt with
    the #779 message and re-asked — never reaches the deploy preflight."""
    from tg_cli import wizard, runner as tg_runner
    monkeypatch.setattr(tg_runner, "list_subnets", lambda *a, **k: [
        {"id": "subnet-0pub0000", "az": "us-east-1a",
         "cidr": "10.0.1.0/24", "public": True, "egress": "public"},
        {"id": "subnet-0nat0000", "az": "us-east-1b",
         "cidr": "10.0.2.0/24", "public": False, "egress": "nat"},
        {"id": "subnet-0pub1111", "az": "us-east-1b",
         "cidr": "10.0.3.0/24", "public": True, "egress": "public"},
    ])
    notes = []
    # 1st pick: mixed (public + nat) → rejected; 2nd: two public → ok.
    picks = [
        ["subnet-0pub0000  us-east-1a  10.0.1.0/24  public(IGW)",
         "subnet-0nat0000  us-east-1b  10.0.2.0/24  nat"],
        ["subnet-0pub0000  us-east-1a  10.0.1.0/24  public(IGW)",
         "subnet-0pub1111  us-east-1b  10.0.3.0/24  public(IGW)"],
    ]
    r = _egress_subnet_resolver(picks, notes)
    out = wizard._ask_subnets(r, {}, "vpc-0abc1234", "us-east-1", None)
    assert out == "subnet-0pub0000,subnet-0pub1111"   # the homogeneous retry
    assert any("mix egress" in n or "mix" in n for n in notes)


def test_subnet_all_private_no_endpoints_rejected(monkeypatch):
    """#959: an all-'none' (private, no NAT) set is rejected unless the
    VPC has the 4 SM/ECR/Logs interface endpoints — mirrors the
    preflight's endpoint check."""
    from tg_cli import wizard, runner as tg_runner
    monkeypatch.setattr(tg_runner, "list_subnets", lambda *a, **k: [
        {"id": "subnet-0none000", "az": "us-east-1a",
         "cidr": "10.0.1.0/24", "public": False, "egress": "none"},
        {"id": "subnet-0none111", "az": "us-east-1b",
         "cidr": "10.0.2.0/24", "public": False, "egress": "none"},
        {"id": "subnet-0nat0000", "az": "us-east-1b",
         "cidr": "10.0.3.0/24", "public": False, "egress": "nat"},
        {"id": "subnet-0nat1111", "az": "us-east-1a",
         "cidr": "10.0.4.0/24", "public": False, "egress": "nat"},
    ])
    # VPC has NO interface endpoints → the all-none pick must be refused.
    monkeypatch.setattr(tg_runner, "vpc_interface_endpoint_count",
                        lambda *a, **k: 0)
    notes = []
    picks = [
        ["subnet-0none000  us-east-1a  10.0.1.0/24  no-egress",
         "subnet-0none111  us-east-1b  10.0.2.0/24  no-egress"],
        ["subnet-0nat0000  us-east-1b  10.0.3.0/24  nat",
         "subnet-0nat1111  us-east-1a  10.0.4.0/24  nat"],
    ]
    r = _egress_subnet_resolver(picks, notes)
    out = wizard._ask_subnets(r, {}, "vpc-0abc1234", "us-east-1", None)
    assert out == "subnet-0nat0000,subnet-0nat1111"   # fell back to NAT pair
    assert any("interface endpoints" in n or "0/4" in n for n in notes)


def test_subnet_all_private_with_endpoints_accepted(monkeypatch):
    """#959: an all-'none' set IS accepted when the VPC has the 4
    interface endpoints (the preflight's endpoint-egress mode)."""
    from tg_cli import wizard, runner as tg_runner
    monkeypatch.setattr(tg_runner, "list_subnets", lambda *a, **k: [
        {"id": "subnet-0none000", "az": "us-east-1a",
         "cidr": "10.0.1.0/24", "public": False, "egress": "none"},
        {"id": "subnet-0none111", "az": "us-east-1b",
         "cidr": "10.0.2.0/24", "public": False, "egress": "none"},
    ])
    monkeypatch.setattr(tg_runner, "vpc_interface_endpoint_count",
                        lambda *a, **k: 4)
    notes = []
    picks = [[
        "subnet-0none000  us-east-1a  10.0.1.0/24  no-egress",
        "subnet-0none111  us-east-1b  10.0.2.0/24  no-egress",
    ]]
    r = _egress_subnet_resolver(picks, notes)
    out = wizard._ask_subnets(r, {}, "vpc-0abc1234", "us-east-1", None)
    assert out == "subnet-0none000,subnet-0none111"
    assert not notes          # accepted, no re-ask


def test_egress_homogeneity_error_helper():
    """Unit: the shared verdict helper matches _byo_egress_preflight's
    rule (homogeneous public/nat OK; mixed → msg; all-none gated on
    endpoints)."""
    from tg_cli import wizard, runner as tg_runner
    # homogeneous → None (valid)
    assert wizard._egress_homogeneity_error(
        {"public"}, "vpc-0abc1234", "us-east-1", None) is None
    assert wizard._egress_homogeneity_error(
        {"nat"}, "vpc-0abc1234", "us-east-1", None) is None
    # mixed → message
    assert wizard._egress_homogeneity_error(
        {"public", "nat"}, "vpc-0abc1234", "us-east-1", None) is not None


# ── #962: upgrade-aware install — deployed params seed the defaults ──
#
# A re-run against a deployed tg-container-stack must read the live CFN
# params and seed them as wizard defaults (an in-place upgrade), frame
# the run as UPGRADE, and LOCK vpc mode (a create-new↔BYO flip can't
# happen in place — the #961 footgun). No stack → today's NEW install,
# byte-identical. These mock the AWS reads (no live calls).


def _stub_deployed(monkeypatch, *, status="UPDATE_COMPLETE", params=None):
    """Stub describe_stack + stack_parameters so deployed_stack_defaults
    runs offline against a synthetic deployed stack."""
    from tg_cli import runner
    monkeypatch.setattr(runner, "describe_stack",
                        lambda *a, **k: {"Status": status, "Outputs": []})
    monkeypatch.setattr(runner, "stack_parameters",
                        lambda *a, **k: (params or {}))


def test_deployed_stack_defaults_maps_params(monkeypatch):
    from tg_cli import runner
    _stub_deployed(monkeypatch, status="UPDATE_COMPLETE", params={
        "EcsImageUri": "public.ecr.aws/e9y1g4o2/tg-container:v1",
        "ExistingVpcId": "",                       # create-new
        "BootstrapAdminEmail": "admin@example.com",
        "AllowedIngressCidr1": "203.0.113.0/24",
        "AllowedIngressCidr2": "198.51.100.7/32",
        "RequireLogin": "true",
        "CertificateArn": "arn:aws:acm:us-east-1:123456789012:cert/x",
    })
    d = runner.deployed_stack_defaults("us-east-1", None)
    assert d["updatable"] is True
    assert d["vpc_mode_create_new"] is True
    a = d["answers"]
    # #1059: EcsImageUri is NO LONGER mapped to answers["image"] (it
    # shadowed the public-image default + re-offered the stale/private
    # deployed ref on upgrade). It still flows out as image_from for the
    # banner + Advanced prefill + behind-check.
    assert "image" not in a
    assert d["image_from"] == "public.ecr.aws/e9y1g4o2/tg-container:v1"
    assert a["bootstrap_email"] == "admin@example.com"
    # CIDR slots joined → the comma-form the validator expects
    assert a["ingress_cidrs"] == "203.0.113.0/24,198.51.100.7/32"
    assert a["enable_login"] == "y"
    # empty ExistingVpcId is NOT carried as a vpc_id default
    assert "vpc_id" not in a


def test_deployed_stack_defaults_byo_carries_vpc(monkeypatch):
    from tg_cli import runner
    _stub_deployed(monkeypatch, params={
        "ExistingVpcId": "vpc-0abc1234",
        "ExistingSubnetIds": "subnet-0aaa1111,subnet-0bbb2222",
        "EcsImageUri": "build",
    })
    d = runner.deployed_stack_defaults("us-east-1", None)
    assert d["vpc_mode_create_new"] is False
    assert d["answers"]["vpc_id"] == "vpc-0abc1234"
    assert d["answers"]["subnet_ids"] == "subnet-0aaa1111,subnet-0bbb2222"


def test_deployed_stack_defaults_none_when_no_stack(monkeypatch):
    from tg_cli import runner
    monkeypatch.setattr(runner, "describe_stack", lambda *a, **k: None)
    assert runner.deployed_stack_defaults("us-east-1", None) is None


def test_deployed_stack_in_progress_is_not_updatable(monkeypatch):
    from tg_cli import runner
    _stub_deployed(monkeypatch, status="UPDATE_IN_PROGRESS", params={})
    d = runner.deployed_stack_defaults("us-east-1", None)
    assert d["updatable"] is False


def test_desired_count_not_in_cli_param_map(monkeypatch):
    """#979: DesiredCount must NOT be in the #962 CLI param→answer map.
    It is NOT a wizard answer — the bash installer (tg-ecs-install.sh)
    owns it via a two-pass dance (pass 1 = 0 ONLY for a fresh service
    that would otherwise hang; pass 2 = the target count) and the #979
    re-run-preserve guard. Mapping it into the CLI defaults would
    resurrect the wizard-vs-installer confusion the #979 stage-down
    came from, so this pins the contract: the count is preserved in
    the installer, never seeded as a CLI default."""
    from tg_cli import runner
    assert "DesiredCount" not in runner._CFN_PARAM_TO_ANSWER
    # And a deployed DesiredCount param is NOT surfaced as an answer.
    _stub_deployed(monkeypatch, params={
        "DesiredCount": "1", "EcsImageUri": "build"})
    d = runner.deployed_stack_defaults("us-east-1", None)
    assert "desired_count" not in d["answers"]
    assert "DesiredCount" not in d["answers"]


def test_ask_vpc_upgrade_skips_questions_create_new():
    """#962: on a detected create-new upgrade, _ask_vpc asks NOTHING —
    it carries the locked create-new (empty vpc_id) forward."""
    from tg_cli import wizard

    class _NoAsk(Resolver):
        def ask(self, q):
            raise AssertionError(f"must not ask on upgrade: {q.key}")

        def ask_multi(self, q):
            raise AssertionError(f"must not ask_multi on upgrade: {q.key}")

    out = wizard._ask_vpc(_NoAsk(interactive=True),
                          {"_is_upgrade": True, "vpc_mode": wizard.VPC_CREATE,
                           "vpc_id": "", "subnet_ids": ""})
    assert out["vpc_id"] == ""
    assert out["subnet_ids"] == ""


def test_ask_vpc_upgrade_byo_carries_locked_vpc():
    """#962: a BYO upgrade carries the deployed VPC/subnets forward
    without re-offering the pick-list."""
    from tg_cli import wizard

    class _NoAsk(Resolver):
        def ask(self, q):
            raise AssertionError("must not ask on upgrade")

    out = wizard._ask_vpc(_NoAsk(interactive=True), {
        "_is_upgrade": True, "vpc_mode": wizard.VPC_EXISTING,
        "vpc_id": "vpc-0abc1234",
        "subnet_ids": "subnet-0aaa1111,subnet-0bbb2222"})
    assert out["vpc_id"] == "vpc-0abc1234"
    assert out["subnet_ids"] == "subnet-0aaa1111,subnet-0bbb2222"


def test_confirm_screen_upgrade_banner():
    """#962: render_confirm frames a detected upgrade as UPGRADE with
    the image from→to; a new install says NEW install."""
    from tg_cli import runner
    up = runner.render_confirm({
        "_is_upgrade": True, "_image_from": "tg-container:v1",
        "image": "tg-container:v2", "vpc_mode": VPC_CREATE,
        "region": "us-east-1"}, {})
    assert "UPGRADE existing tg-container-stack" in up
    assert "tg-container:v1  →  tg-container:v2" in up
    new = runner.render_confirm({"vpc_mode": VPC_CREATE,
                                 "region": "us-east-1"}, {})
    assert "NEW install" in new


def test_transient_upgrade_keys_not_persisted():
    """#962: the _is_upgrade/_image_from markers are run-only — they
    must never be written to the resume config (a later greenfield
    re-run would wrongly think it's an upgrade)."""
    from tg_cli import config
    safe = config._scrub({
        "region": "us-east-1", "_is_upgrade": True,
        "_image_from": "x", "bootstrap_password": "secret"})
    assert "region" in safe
    assert "_is_upgrade" not in safe
    assert "_image_from" not in safe
    assert "bootstrap_password" not in safe   # existing secret scrub


# ──────────────── #796 (#782): Cognito wizard path ─────────────────
#
# The wizard must mirror the shell installer's #782 fix: cognito is the
# default base login, and on cognito the OIDC trio must NOT be asked
# (the installer derives it from tg-cognito-pool). Demanding an issuer
# was the bug that blocked the --non-interactive cognito install.


def test_cognito_path_does_not_ask_oidc():
    """provider=cognito → no oidc_issuer/client_id questions (so a
    --non-interactive install doesn't block on a missing issuer)."""
    r = Resolver(interactive=True, scripted=_scripted(
        auth_provider=AUTH_COGNITO))
    answers = run_questions(r, {"account_id": "123456789012"})
    assert answers["auth_provider"] == AUTH_COGNITO
    assert not answers.get("oidc_issuer")
    assert not answers.get("oidc_client_id")


def test_cognito_env_sets_provider_and_no_oidc():
    """provider=cognito → TG_AUTH_PROVIDER=cognito and NO TG_OIDC_* env
    (the installer fills those from the pool)."""
    r = Resolver(interactive=True, scripted=_scripted(
        auth_provider=AUTH_COGNITO))
    answers = run_questions(r, {"account_id": "123456789012"})
    e2 = to_env(answers, phase=2)
    assert e2["TG_AUTH_REQUIRE_LOGIN"] == "1"
    assert e2["TG_AUTH_PROVIDER"] == "cognito"
    assert "TG_OIDC_ISSUER" not in e2
    assert "TG_OIDC_CLIENT_ID" not in e2


def test_cognito_install_dry_run_non_interactive(tmp_path):
    """#796 regression on the REAL binary: TG_AUTH_PROVIDER=cognito with
    NO OIDC issuer must NOT fail '--non-interactive: no value for
    required oidc_issuer'."""
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "203.0.113.0/24",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_AUTH_PROVIDER": "cognito",
        # deliberately NO TG_OIDC_ISSUER — the cognito path must not need it
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"],
                env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "no value for required 'oidc_issuer'" not in r.stderr


def test_cognito_prod_install_is_single_phase_login_on(tmp_path):
    """#860 regression on the REAL binary: the DEFAULT install path —
    prod environment (no TG_ENVIRONMENT override) + Cognito — must be
    SINGLE-phase with login ON. Routing it through the Okta-only
    two-phase bootstrap forced TG_AUTH_REQUIRE_LOGIN=0 in phase 1,
    which the shell installer's prod login-off hard-fail rejects, so
    the install could never complete. The dry-run must show login ON
    (TG_AUTH_REQUIRE_LOGIN=1), NOT the phase-1 login-off env, and NOT
    the two-phase pause text."""
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "203.0.113.0/24",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_AUTH_PROVIDER": "cognito",
        # no TG_ENVIRONMENT → prod; no TG_OIDC_ISSUER → cognito
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"],
                env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    low = out.lower()
    # login ON, single-phase — the phase-2 env, never the phase-1
    # login-off the prod hard-fail would reject.
    assert "TG_AUTH_REQUIRE_LOGIN=1" in out
    assert "TG_AUTH_REQUIRE_LOGIN=0" not in out
    assert "two-phase" not in low
    assert "single-phase" in low


@pytest.mark.parametrize("mode,scheme", [
    (CERT_EXISTING, "https"),
    (CERT_SELFSIGNED, "https"),
    (CERT_PLAINTEXT, "http"),
])
def test_redirect_uri_scheme_follows_cert_choice(mode, scheme):
    answers = {"cert_mode": mode}
    assert cert_scheme(answers) == scheme
    uri = redirect_uri(answers, "my-alb-123.us-east-1.elb.amazonaws.com")
    assert uri == f"{scheme}://my-alb-123.us-east-1.elb.amazonaws.com/auth/callback"


def test_redirect_uri_prefers_custom_domain():
    answers = {"cert_mode": CERT_EXISTING, "domain_name": "tg.example.com"}
    assert redirect_uri(answers, "raw-alb.elb.amazonaws.com") == \
        "https://tg.example.com/auth/callback"


def test_phase_state_persists_for_resume(tmp_path, monkeypatch):
    """A persisted phase=awaiting-oidc-registration is what a re-run reads."""
    monkeypatch.setenv("TG_CONFIG_HOME", str(tmp_path))
    import importlib
    importlib.reload(config)
    config.save({
        "region": "us-east-1",
        "phase": "awaiting-oidc-registration",
        "oidc_redirect_uri": "https://x/auth/callback",
        "oidc_client_secret": "NOPE",
    })
    loaded = config.load()
    assert loaded["phase"] == "awaiting-oidc-registration"
    assert loaded["oidc_redirect_uri"] == "https://x/auth/callback"
    assert "oidc_client_secret" not in loaded  # secret never persisted
    monkeypatch.delenv("TG_CONFIG_HOME", raising=False)
    importlib.reload(config)


def test_real_cli_dry_run_is_cognito_single_phase(tmp_path):
    """#926: the real --dry-run plan is Cognito-only single-phase — no
    two-phase pause — even when stale TG_OIDC_* env is present (the
    installer ignores it; SAML is post-install)."""
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "203.0.113.0/24",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_OIDC_ISSUER": "https://example.okta.com",
        "TG_OIDC_CLIENT_ID": "client-abc",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"], env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout.lower()
    assert "two-phase" not in out
    assert "single-phase" in out
    assert "tg_auth_provider=cognito" in out  # pinned Cognito
    assert "tg_auth_require_login=1" in out    # login ON, single-phase


# ──────────── L1/L2: #530 phase-2 delegating flags ─────────────

def test_real_cli_local_dry_run_picks_compose_path(tmp_path):
    """--local routes to the docker-compose installer + skips the
    ECS cert/OIDC two-phase."""
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "10.0.0.0/8",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "a@b.co",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_AUTH_REQUIRE_LOGIN": "0",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive", "--local"],
                env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout.lower()
    assert "--local" in out and "tg-local-install.sh" in out
    assert "no resources created" in out


def _install_dry_env():
    return {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "10.0.0.0/8",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "a@b.co",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_AUTH_REQUIRE_LOGIN": "0",
    }


def test_real_cli_dry_run_deploys_cur_by_default(tmp_path):
    """#922/#1075: a default install plans to deploy CUR (required —
    the sole spend source); the plan never says SKIPPED."""
    r = _run_tg(["install", "--dry-run", "--non-interactive"],
                _install_dry_env(), tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "CUR 2.0 + Athena" in out
    assert "SKIPPED" not in out
    assert "admin-ui-publish.sh" not in out


def test_real_cli_no_cur_flag_is_removed(tmp_path):
    """#1075: --no-cur is removed — passing it is an argparse error
    (non-zero exit), so a script that opts out fails loudly."""
    r = _run_tg(["install", "--dry-run", "--non-interactive", "--no-cur"],
                _install_dry_env(), tmp_path)
    assert r.returncode != 0
    # argparse reports the unrecognized argument on stderr.
    assert "no-cur" in (r.stderr + r.stdout)


def test_real_cli_with_cur_is_noop_alias(tmp_path):
    """#1075: --with-cur still parses (deprecated no-op) — existing
    scripts passing it don't error, and CUR still plans to deploy."""
    r = _run_tg(["install", "--dry-run", "--non-interactive",
                 "--with-cur", "--verify"],
                _install_dry_env(), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "CUR 2.0 + Athena" in r.stdout


# ── #1075: CUR is REQUIRED — no opt-out, and a CUR failure is FATAL ──
#
# CUR (tg-cur-athena) is the sole spend source (#720). On a cloud
# install it always runs and a deploy failure propagates non-zero (the
# install is not complete without it). --local is EXEMPT (no cloud CUR
# from a docker-compose dev install). A stale TG_SKIP_CUR fails fast.


class _Args:
    """Minimal stand-in for the argparse Namespace _run_addons reads.
    #1075: no_cur is gone; only `local` remains."""
    def __init__(self, local=False):
        self.local = local


def test_run_addons_cur_failure_is_fatal(monkeypatch):
    """#1075: a CUR deploy failure on a CLOUD install propagates the
    non-zero rc (it is NOT swallowed) — the install reports failure."""
    import tg_cli.__main__ as m
    monkeypatch.delenv("TG_SKIP_CUR", raising=False)
    # deploy fails (rc=2) → fatal; verify shouldn't even run.
    monkeypatch.setattr(m.runner, "run_captured",
                        lambda script, env: (2, "boom"))
    rc = m._run_addons(_Args(local=False), {})
    assert rc == 2           # fatal — propagates non-zero


def test_run_addons_local_is_exempt(monkeypatch):
    """#1075 A.1: --local never deploys the real (cloud) CUR stack —
    _run_addons is a no-op and run_captured is never called."""
    import tg_cli.__main__ as m
    monkeypatch.delenv("TG_SKIP_CUR", raising=False)
    monkeypatch.setattr(m.runner, "run_captured",
                        lambda script, env: (_ for _ in ()).throw(
                            AssertionError("CUR must not run on --local")))
    assert m._run_addons(_Args(local=True), {}) == 0


def test_run_addons_quiet_on_success(monkeypatch, capsys):
    """#996: on the happy path the CUR deploy + verify output is
    CAPTURED (not streamed) — none of the Athena SQL / result-dump
    chatter reaches the terminal; rc 0."""
    import tg_cli.__main__ as m
    monkeypatch.delenv("TG_SKIP_CUR", raising=False)
    noisy = ("==> Results:\nemail\tactual_usd\tline_items\n"
             "What happens next: SELECT line_item_iam_principal …")
    monkeypatch.setattr(m.runner, "run_captured",
                        lambda script, env: (0, noisy))
    rc = m._run_addons(_Args(local=False), {})
    assert rc == 0
    captured = capsys.readouterr()
    # the verbose CUR/Athena chatter must NOT have been printed
    assert "Results:" not in captured.out
    assert "line_item_iam_principal" not in captured.out
    assert "What happens next" not in captured.out


def test_run_addons_replays_captured_output_on_failure(monkeypatch, capsys):
    """#1075: a CUR-deploy failure replays the captured output (the
    cause) to stderr with the idempotent re-run message — actionable,
    and now fatal."""
    import tg_cli.__main__ as m
    monkeypatch.delenv("TG_SKIP_CUR", raising=False)
    monkeypatch.setattr(m.runner, "run_captured",
                        lambda script, env: (1, "ATHENA stack CREATE_FAILED xyz"))
    rc = m._run_addons(_Args(local=False), {})
    assert rc == 1           # fatal
    err = capsys.readouterr().err
    assert "CREATE_FAILED xyz" in err        # the captured cause surfaced
    assert "CUR deploy failed" in err
    assert "re-run `tg install`" in err      # idempotent re-run promise


def test_run_addons_stale_skip_cur_fails_fast(monkeypatch):
    """#1075: TG_SKIP_CUR is no longer honored — a cloud install with it
    set fails fast with a clear message (SystemExit), never silently
    runs/skips CUR."""
    import tg_cli.__main__ as m
    monkeypatch.setenv("TG_SKIP_CUR", "1")
    monkeypatch.setattr(m.runner, "run_captured",
                        lambda script, env: (_ for _ in ()).throw(
                            AssertionError("CUR must not run before the guard")))
    with pytest.raises(SystemExit) as ei:
        m._run_addons(_Args(local=False), {})
    assert "TG_SKIP_CUR is no longer supported" in str(ei.value)


# ── #1067: a cosmetic summary-print failure must not skip CUR ──
#
# The installer's post-"Done" summary is decorative; a stray heredoc
# backslash (demo2 96c4e4c) made tg-ecs-install.sh exit non-zero AFTER a
# healthy core install, which flipped cmd_install to the failure branch
# and skipped CUR. _core_stack_healthy keys core-install health off the
# CFN stack status (the assert_stack_succeeded source of truth), not the
# wrapper's exit code, so a green core proceeds to CUR regardless.


def test_core_stack_healthy_true_on_complete(monkeypatch):
    """A CREATE/UPDATE_COMPLETE tg-container-stack → healthy."""
    import tg_cli.__main__ as m
    for status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        monkeypatch.setattr(m.runner, "describe_stack",
                            lambda *a, **k: {"Status": status})
        assert m._core_stack_healthy({"region": "us-east-1"}) is True


def test_core_stack_healthy_false_on_rollback_or_missing(monkeypatch):
    """A rollback/failed status — or an unreadable stack — is NOT healthy,
    so a genuine core-stack failure still falls through to the failure
    path (no false 'healthy' claim)."""
    import tg_cli.__main__ as m
    for status in ("UPDATE_ROLLBACK_COMPLETE", "CREATE_FAILED",
                   "ROLLBACK_COMPLETE"):
        monkeypatch.setattr(m.runner, "describe_stack",
                            lambda *a, **k: {"Status": status})
        assert m._core_stack_healthy({}) is False
    # probe failure (None) → not healthy
    monkeypatch.setattr(m.runner, "describe_stack", lambda *a, **k: None)
    assert m._core_stack_healthy({}) is False


def test_real_cli_destroy_local_dry_run(tmp_path):
    r = _run_tg(["destroy", "--local", "--dry-run"],
                {"AWS_REGION": "us-east-1"}, tmp_path)
    assert r.returncode == 0
    assert "tg-local-destroy.sh" in r.stdout
    assert "nothing deleted" in r.stdout.lower()


def test_runner_exposes_delegating_wrappers():
    """The new engine-script delegations resolve to real,
    published (non-internal) script paths."""
    from tg_cli import runner
    # #576: ADMIN_PUBLISH_SH removed with the desktop client.
    for const in (runner.LOCAL_INSTALL_SH, runner.LOCAL_DESTROY_SH,
                  runner.CUR_DEPLOY_SH, runner.CUR_DESTROY_SH,
                  runner.VERIFY_CUR_SH):
        assert const.exists(), f"{const} missing"
        assert "/internal/" not in str(const), \
            f"{const} must stay published (#527 hard constraint)"


def test_real_cli_destroy_full_dry_run_mentions_cur(tmp_path):
    """#922: --full destroy must tear down tg-cur-athena too (CUR is
    default-on now, so it would otherwise orphan)."""
    r = _run_tg(["destroy", "--full", "--dry-run"],
                {"AWS_REGION": "us-east-1"}, tmp_path)
    assert r.returncode == 0
    assert "tg-cur-athena" in r.stdout


def test_local_dry_run_omits_ecs_only_questions(tmp_path):
    """#530 phase-2 regression (tg-ops): --local --non-interactive
    must NOT require ingress_cidrs / image (ECS-only) — it used to
    hard-fail 'no value for required ingress_cidrs'."""
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_AUTH_REQUIRE_LOGIN": "0",
        # NOTE: deliberately NO TG_ALLOWED_INGRESS_CIDRS / cert.
    }
    r = _run_tg(["install", "--local", "--dry-run", "--non-interactive"],
                env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    # reduced confirm screen: no ECS/ALB rows
    assert "Ingress CIDRs" not in out
    assert "Always created" not in out
    assert "docker-compose" in out
    # the env it would export omits the ALB allowlist
    assert "TG_ALLOWED_INGRESS_CIDRS" not in out


def test_local_question_set_skips_cert_and_cidrs():
    """L1: run_questions(local=True) collects neither ingress CIDRs
    nor a cert mode."""
    r = Resolver(interactive=True, scripted=_scripted())
    answers = run_questions(r, {"account_id": "123456789012"}, local=True)
    assert "ingress_cidrs" not in answers
    assert "image" not in answers
    assert "cert_mode" not in answers
    # but the shared keys are still collected
    assert answers["region"] == "us-east-1"
    assert answers["bootstrap_email"] == "admin@example.com"


# ────────────────────── #774: BYO-VPC wizard + env ──────────────────

@pytest.mark.parametrize("val,ok", [
    ("", True),                              # empty = create-new
    ("vpc-0abc1234", True),
    ("vpc-0123456789abcdef0", True),
    ("vpc-xyz", False),
    ("notavpc", False),
])
def test_vpc_id_validator(val, ok):
    assert (V.vpc_id(val) is None) == ok


@pytest.mark.parametrize("val,ok", [
    ("subnet-0aaa1111,subnet-0bbb2222", True),
    ("subnet-0aaa1111", False),             # need >=2
    ("subnet-0aaa1111,bogus", False),
    ("", False),
])
def test_subnet_ids_validator(val, ok):
    assert (V.subnet_ids(val) is None) == ok


def test_create_new_vpc_emits_no_vpc_env():
    # The default vpc_mode=create-new must leave the greenfield path
    # byte-identical: no TG_VPC_ID / TG_SUBNET_IDS in the env.
    r = Resolver(interactive=True, scripted=_scripted())
    answers = run_questions(r, {"account_id": "123456789012"})
    env = to_env(answers)
    assert "TG_VPC_ID" not in env
    assert "TG_SUBNET_IDS" not in env


def test_byo_vpc_maps_to_env():
    # use-existing + scripted vpc/subnet picks → TG_VPC_ID/SUBNET_IDS.
    # Scripted answers short-circuit the live describe-* pick-list.
    r = Resolver(interactive=True, scripted=_scripted(
        vpc_mode=VPC_EXISTING,
        vpc_id="vpc-0abc1234",
        subnet_ids="subnet-0aaa1111,subnet-0bbb2222",
    ))
    answers = run_questions(r, {"account_id": "123456789012"})
    env = to_env(answers)
    assert env["TG_VPC_ID"] == "vpc-0abc1234"
    assert env["TG_SUBNET_IDS"] == "subnet-0aaa1111,subnet-0bbb2222"


def test_byo_vpc_preseeded_from_env_supplied():
    # When TG_VPC_ID is pre-supplied (env), a non-interactive run
    # carries it through to the CFN env without prompting.
    r = Resolver(interactive=False, supplied=_scripted(
        vpc_id="vpc-0ddd4444",
        subnet_ids="subnet-0eee5555,subnet-0fff6666",
    ))
    answers = run_questions(r, {"account_id": "123456789012"})
    env = to_env(answers)
    assert env["TG_VPC_ID"] == "vpc-0ddd4444"
    assert env["TG_SUBNET_IDS"] == "subnet-0eee5555,subnet-0fff6666"


# ── #961: don't offer tg's OWN VPC as a BYO 'existing VPC' ───────────
#
# list_vpcs runs an unfiltered describe-vpcs, so a re-run's BYO
# pick-list listed the VPC tg itself created (create-new path). Picking
# it flipped tg-container-stack create-new→BYO in place — which CFN
# can't do (RDS/ALB already live in those subnets) → UPDATE_FAILED +
# rollback. The wizard must exclude tg-managed VPCs from the BYO
# choices (a tg-managed VPC carries the aws:cloudformation:stack-name=
# tg-container-stack tag, or Name=tg-vpc).


def test_vpc_is_tg_managed_by_stack_tag_or_name():
    from tg_cli import runner
    # CFN stack-name tag → managed
    assert runner.vpc_is_tg_managed(
        {"id": "vpc-1", "stack_name": "tg-container-stack"}) is True
    # tg-vpc Name tag → managed
    assert runner.vpc_is_tg_managed(
        {"id": "vpc-2", "name": "tg-vpc"}) is True
    # a genuine external VPC → not managed
    assert runner.vpc_is_tg_managed(
        {"id": "vpc-3", "name": "corp-shared", "stack_name": None}) is False
    # some OTHER stack's VPC → not tg's
    assert runner.vpc_is_tg_managed(
        {"id": "vpc-4", "stack_name": "some-other-stack"}) is False
    assert runner.vpc_is_tg_managed({}) is False


def _vpc_picker(vpcs, want_substr, notes_sink):
    """A Resolver that stubs runner.list_vpcs to `vpcs`, answers the
    vpc_mode question with 'existing', the vpc_pick menu by substring,
    and subnet_ids via scripted; records the offered pick choices +
    notes. Returns (resolver, get_choices)."""
    from tg_cli.prompts import Resolver as _R
    seen = {"choices": None}

    class _P(_R):
        def __init__(self):
            super().__init__(interactive=True, scripted=_scripted(
                vpc_mode=VPC_EXISTING,
                subnet_ids="subnet-0aaa1111,subnet-0bbb2222",
            ))

        def note(self, msg):
            notes_sink.append(msg)

        def ask(self, q):
            if q.key == "vpc_pick":
                seen["choices"] = list(q.choices)
                for c in q.choices:
                    if want_substr in c:
                        return c
                raise AssertionError(
                    f"no choice matched {want_substr!r}: {q.choices}")
            return super().ask(q)

    return _P(), (lambda: seen["choices"])


def test_ask_vpc_excludes_tg_managed_from_picklist(monkeypatch):
    """A tg-managed VPC must NOT appear as a selectable BYO choice when
    a real external VPC is also present."""
    from tg_cli import wizard, runner
    vpcs = [
        {"id": "vpc-0aaaaaaa", "cidr": "10.0.0.0/16", "name": "tg-vpc",
         "stack_name": "tg-container-stack", "default": False,
         "tg_managed": True},
        {"id": "vpc-0ccccccc", "cidr": "172.16.0.0/16", "name": "corp",
         "stack_name": None, "default": False, "tg_managed": False},
    ]
    monkeypatch.setattr(runner, "list_vpcs", lambda *a, **k: vpcs)
    # subnets stub so _ask_subnets (scripted) doesn't hit AWS
    monkeypatch.setattr(runner, "list_subnets", lambda *a, **k: [])
    notes = []
    r, choices = _vpc_picker(vpcs, "vpc-0ccccccc", notes)
    out = wizard._ask_vpc(r, {"region": "us-east-1"})
    assert out["vpc_id"] == "vpc-0ccccccc"
    offered = choices()
    assert not any("vpc-0aaaaaaa" in c for c in offered)   # excluded
    assert any("vpc-0ccccccc" in c for c in offered)


def test_ask_vpc_managed_only_is_flagged_and_reasked(monkeypatch):
    """Edge case: when tg's own VPC is the ONLY one in the account it
    is shown but flagged; picking it warns and re-asks (never silently
    carries the destructive mode-flip)."""
    from tg_cli import wizard, runner
    vpcs = [
        {"id": "vpc-0aaaaaaa", "cidr": "10.0.0.0/16", "name": "tg-vpc",
         "stack_name": "tg-container-stack", "default": False,
         "tg_managed": True},
    ]
    monkeypatch.setattr(runner, "list_vpcs", lambda *a, **k: vpcs)
    monkeypatch.setattr(runner, "list_subnets", lambda *a, **k: [])

    notes = []
    calls = {"mode": 0, "pick": 0}
    from tg_cli.prompts import Resolver as _R

    # Drive the flow statefully: 1st vpc_mode answer = 'existing' (→ the
    # managed-only pick-list), pick the flagged managed VPC → warn +
    # re-ask; on the re-ask choose create-new to escape (the realistic
    # recovery the warning steers toward).
    class _P(_R):
        def __init__(self):
            super().__init__(interactive=True, scripted=_scripted())

        def note(self, msg):
            notes.append(msg)

        def ask(self, q):
            if q.key == "vpc_mode":
                calls["mode"] += 1
                return VPC_EXISTING if calls["mode"] == 1 else VPC_CREATE
            if q.key == "vpc_pick":
                calls["pick"] += 1
                # the flagged managed VPC is offered (only choice)
                assert any("tg-managed" in c for c in q.choices)
                return q.choices[0]   # pick it → should warn + re-ask
            return super().ask(q)

    out = wizard._ask_vpc(_P(), {"region": "us-east-1"})
    assert calls["pick"] == 1                    # picked managed once
    assert calls["mode"] == 2                    # re-asked the whole flow
    assert any("tg's own VPC" in n for n in notes)
    # re-ask → create-new escape: empty vpc_id (greenfield), not tg's VPC
    assert out["vpc_id"] == ""


def test_ask_vpc_warns_when_supplied_id_is_tg_managed(monkeypatch):
    """A pre-supplied vpc_id resolving to tg's own VPC warns (doesn't
    hard-block — a scripted edge case may mean it) and carries the id."""
    from tg_cli import wizard, runner
    monkeypatch.setattr(runner, "list_vpcs", lambda *a, **k: [
        {"id": "vpc-0aaaaaaa", "name": "tg-vpc",
         "stack_name": "tg-container-stack", "tg_managed": True},
    ])
    notes = []
    from tg_cli.prompts import Resolver as _R

    class _P(_R):
        def note(self, msg):
            notes.append(msg)

    r = _P(interactive=False, supplied=_scripted(
        vpc_id="vpc-0aaaaaaa",
        subnet_ids="subnet-0aaa1111,subnet-0bbb2222",
    ))
    out = wizard._ask_vpc(r, {"region": "us-east-1"})
    assert out["vpc_id"] == "vpc-0aaaaaaa"           # carried, not blocked
    assert any("tg-managed" in n for n in notes)


def test_confirm_screen_create_new_says_vpc_created():
    # #778: create-new (no vpc_id) confirm screen promises a VPC.
    from tg_cli import runner
    out = runner.render_confirm({"vpc_mode": VPC_CREATE}, {})
    assert "Always created    : VPC (2-AZ), RDS Postgres, ALB" in out


def test_confirm_screen_byo_vpc_does_not_say_vpc_created():
    # #778: on BYO-VPC the go/no-go screen must NOT claim a VPC is
    # created — it must name the reused VPC + subnets instead. The
    # live bug: it hardcoded "Always created: VPC (2-AZ)" even when
    # reusing an existing VPC, contradicting the actual plan.
    from tg_cli import runner
    answers = {
        "vpc_mode": VPC_EXISTING,
        "vpc_id": "vpc-0abc1234",
        "subnet_ids": "subnet-0aaa1111,subnet-0bbb2222",
    }
    out = runner.render_confirm(answers, {})
    assert "Always created" not in out
    assert "VPC (2-AZ)" not in out
    assert "into existing VPC vpc-0abc1234" in out
    assert "subnet-0aaa1111,subnet-0bbb2222" in out


# ──────────────── L1+L2: #881 resume disclosure + replay ────────────

def test_resume_summary_lists_nonsecret_collected_keys():
    """#881: resume_summary replays collected, non-secret answers in the
    label-map order; empty/missing keys are skipped."""
    from tg_cli import runner
    answers = {
        "region": "us-east-1",
        "account_id": "123456789012",
        "subnet_ids": "subnet-0a,subnet-0b",
        "ingress_cidrs": "",          # empty → skipped
        "bootstrap_email": "admin@example.com",
    }
    out = runner.resume_summary(answers)
    assert "Resuming — collected so far:" in out
    assert "Region" in out and "us-east-1" in out
    assert "Account" in out and "123456789012" in out
    assert "Subnets" in out and "subnet-0a,subnet-0b" in out
    assert "admin@example.com" in out
    # empty ingress_cidrs is not shown
    assert "Ingress CIDRs" not in out
    # region precedes account precedes subnets (label-map order)
    assert out.index("Region") < out.index("Account") < out.index("Subnets")
    assert "Continuing with the remaining questions" in out


def test_resume_summary_filters_secrets():
    """#881: even if a secret is stuffed into answers, it's never echoed."""
    from tg_cli import runner, config
    answers = {
        "region": "us-east-1",
        "oidc_client_secret": "SHOULD-NEVER-APPEAR",
        "TG_OIDC_CLIENT_SECRET": "ALSO-NOT",
    }
    out = runner.resume_summary(answers, config.SECRET_KEYS)
    assert "SHOULD-NEVER-APPEAR" not in out
    assert "ALSO-NOT" not in out
    # the non-secret key still shows
    assert "us-east-1" in out


def test_resume_summary_empty_when_nothing_collected():
    """#881: no collected keys → empty string (caller skips printing)."""
    from tg_cli import runner
    assert runner.resume_summary({}) == ""
    assert runner.resume_summary({"unknown_key": "x"}) == ""


def test_real_cli_discloses_state_location_every_run(tmp_path):
    """#881: every install run prints where state is saved + --full-reset,
    even a fresh non-interactive dry-run."""
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": "123456789012",
        "TG_ALLOWED_INGRESS_CIDRS": "203.0.113.0/24",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        "TG_OIDC_ISSUER": "https://example.okta.com",
        "TG_OIDC_CLIENT_ID": "client-abc",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"], env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "config.json" in r.stdout
    assert "--full-reset" in r.stdout


def test_real_cli_resume_replays_collected_answers(tmp_path):
    """#881: with an account-keyed config already on disk (a prior
    interrupted run), the re-run prints the 'collected so far' summary
    before continuing. Drives the REAL binary so the resume path is
    exercised end-to-end (a unit test can't catch the cmd_install wiring)."""
    acct = "123456789012"
    # Seed an account-keyed config as if a prior run had been interrupted
    # after Region/Account/Subnets (config-<acct>.json is what cmd_install
    # reloads once the account is known).
    saved = {
        "account_id": acct,
        "region": "us-east-1",
        "subnet_ids": "subnet-0aaa1111,subnet-0bbb2222",
        "vpc_id": "vpc-0abc1234",
    }
    (tmp_path / f"config-{acct}.json").write_text(json.dumps(saved))
    # Provide the remaining required answers via env so the non-interactive
    # dry-run completes; the seeded keys should be REPLAYED, not re-asked.
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": acct,
        "TG_ALLOWED_INGRESS_CIDRS": "203.0.113.0/24",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": f"arn:aws:acm:us-east-1:{acct}:certificate/x",
        "TG_OIDC_ISSUER": "https://example.okta.com",
        "TG_OIDC_CLIENT_ID": "client-abc",
        "TG_VPC_ID": "vpc-0abc1234",
        "TG_SUBNET_IDS": "subnet-0aaa1111,subnet-0bbb2222",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive"], env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Resuming — collected so far:" in r.stdout
    assert "subnet-0aaa1111,subnet-0bbb2222" in r.stdout


def test_real_cli_full_reset_does_not_resume(tmp_path):
    """#881: --full-reset ignores saved state, so NO resume summary."""
    acct = "123456789012"
    (tmp_path / f"config-{acct}.json").write_text(
        json.dumps({"account_id": acct, "region": "us-east-1",
                    "subnet_ids": "subnet-0aaa1111,subnet-0bbb2222"}))
    env_extra = {
        "AWS_REGION": "us-east-1",
        "TG_TARGET_ACCOUNT_ID": acct,
        "TG_ALLOWED_INGRESS_CIDRS": "203.0.113.0/24",
        "TG_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "TG_CERT_ARN": f"arn:aws:acm:us-east-1:{acct}:certificate/x",
        "TG_OIDC_ISSUER": "https://example.okta.com",
        "TG_OIDC_CLIENT_ID": "client-abc",
    }
    r = _run_tg(["install", "--dry-run", "--non-interactive", "--full-reset"],
                env_extra, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Resuming — collected so far:" not in r.stdout
    # but the state-location line still prints (every run)
    assert "config.json" in r.stdout


# ── #921: bootstrap admin password (Option A random / B provided) ──


def test_bootstrap_password_blank_emits_no_env():
    """Option A (blank): no TG_BOOTSTRAP_ADMIN_PASSWORD in the env —
    the installer then sets a random throwaway + confirms the user."""
    env = to_env(_scripted(bootstrap_password=""))
    assert "TG_BOOTSTRAP_ADMIN_PASSWORD" not in env


def test_bootstrap_password_provided_maps_to_env():
    """Option B: a provided password threads to the installer env."""
    env = to_env(_scripted(bootstrap_password="Sup3rSecretPw1"))
    assert env["TG_BOOTSTRAP_ADMIN_PASSWORD"] == "Sup3rSecretPw1"


def test_bootstrap_password_never_persisted_to_config():
    """The password is a SECRET — config._scrub must strip both the
    answer key and its env form before anything is written to disk."""
    scrubbed = config._scrub(_scripted(bootstrap_password="Sup3rSecretPw1"))
    assert "bootstrap_password" not in scrubbed
    # and the env-form key is covered too (defense in depth).
    assert "TG_BOOTSTRAP_ADMIN_PASSWORD" in config.SECRET_KEYS
    assert "bootstrap_password" in config.SECRET_KEYS


def test_admin_password_validator_policy():
    """The policy validator mirrors the Cognito pool: ≥12 + lower +
    upper + digit; blank is allowed (means 'generate a random one')."""
    assert V.admin_password("") is None            # blank ok (Option A)
    assert V.admin_password("Sup3rSecretPw1") is None
    assert V.admin_password("short1A") is not None        # too short
    assert V.admin_password("alllowercase123") is not None  # no upper
    assert V.admin_password("ALLUPPERCASE123") is not None  # no lower
    assert V.admin_password("NoDigitsHereAtAll") is not None  # no digit


# ── #1018: cert existence check must run AFTER the account preflight ──
# A wrong-account install used to fail at the cert `describe` (under the
# unintended account) before the account-mismatch hard-fail fired — a
# red herring. The fix reorders the cert block to run after the
# Pre-flight block. The live shell ordering is owner-smoked, but the
# SOURCE-order invariant is a real regression guard a static test can
# prove: in tg-ecs-install.sh, the account preflight + mismatch hard-
# fail must appear BEFORE the cert `describe-certificate` call.
_INSTALL_SH = _REPO / "scripts" / "tg-ecs-install.sh"


def test_cert_check_runs_after_account_preflight():
    src = _INSTALL_SH.read_text()
    preflight = src.index('step "Pre-flight checks"')
    # the non-overridable wrong-account hard-fail
    mismatch = src.index('resolve to account')
    # the cert existence describe (the thing that must come later)
    cert = src.index("aws acm describe-certificate")
    assert preflight < mismatch < cert, (
        "cert describe must run AFTER the Pre-flight account "
        "mismatch hard-fail (#1018 reorder)"
    )


def test_cert_not_found_error_names_queried_account_not_target():
    """#1018: the 'cert not found' message must report the account
    actually queried ($CALLER_ACCT) and the ARN's own account — NOT the
    old misleading ${CALLER_ACCT:-$TG_TARGET_ACCOUNT_ID} fallback that
    printed the configured target even when creds resolved elsewhere."""
    src = _INSTALL_SH.read_text()
    # the old fallback form must be gone from the cert error
    assert "${CALLER_ACCT:-$TG_TARGET_ACCOUNT_ID}, region" not in src
    # the new message names the queried account + the ARN's account
    assert "queried account" in src
    assert "ARN names account" in src
    assert "CERT_ARN_ACCT=$(printf" in src


def test_aws_profile_unset_stays_soft_warn_not_required():
    """#1018 (revised scope): require-AWS_PROFILE was DROPPED. The
    unset-profile path stays a soft `warn` + default-chain fallback
    (#768) — regression guard that no hard-require was introduced."""
    src = _INSTALL_SH.read_text()
    # the soft-warn for an unset profile is still present...
    assert "AWS_PROFILE not set" in src
    # ...and it is a warn, not a fail (no hard-require crept in).
    warn_idx = src.index("AWS_PROFILE not set")
    line_start = src.rfind("\n", 0, warn_idx) + 1
    assert src[line_start:warn_idx].lstrip().startswith("warn ")


# ───────────── #1087: AWS_PROFILE / SSO preflight (runner) ─────────────
# preflight_caller is the wizard's testable seam: it resolves the
# credential source, runs ONE read-only get-caller-identity (the
# universal liveness probe), and returns a dict the wizard acts on.
# subprocess.run is monkeypatched so CI exercises every branch with NO
# real AWS.

class _FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _fake_aws(identity=None, sso_value="", caller_fails=False):
    """Build a subprocess.run replacement dispatching on the aws cmd:
    `aws configure get …` → sso_value; `aws sts get-caller-identity`
    → identity JSON (or failure)."""
    def _run(cmd, *a, **k):
        if "configure" in cmd and "get" in cmd:
            return _FakeProc(0 if sso_value else 1, sso_value)
        if "get-caller-identity" in cmd:
            if caller_fails:
                return _FakeProc(255, "")
            return _FakeProc(0, json.dumps(identity or {}))
        return _FakeProc(0, "")
    return _run


def test_preflight_caller_success_reports_identity(monkeypatch):
    import tg_cli.runner as runner
    ident = {"Account": "123456789012",
             "Arn": "arn:aws:sts::123456789012:assumed-role/r/sess"}
    monkeypatch.setattr(runner.subprocess, "run", _fake_aws(identity=ident))
    monkeypatch.setenv("AWS_PROFILE", "tg-install-dev")
    out = runner.preflight_caller({"AWS_PROFILE": "tg-install-dev"})
    assert out["ok"] is True
    assert out["account"] == "123456789012"
    assert out["arn"].endswith("/sess")
    assert out["source"] == "profile tg-install-dev"


def test_preflight_caller_unset_profile_uses_default_chain(monkeypatch):
    import tg_cli.runner as runner
    ident = {"Account": "123456789012", "Arn": "arn:aws:iam::1:user/u"}
    monkeypatch.setattr(runner.subprocess, "run", _fake_aws(identity=ident))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    out = runner.preflight_caller({})
    assert out["ok"] is True
    assert out["profile"] is None
    assert "default credential chain" in out["source"]


def test_preflight_caller_failure_signals_not_ok(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_aws(caller_fails=True))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    out = runner.preflight_caller({})
    assert out["ok"] is False
    assert out["account"] is None


def test_preflight_caller_detects_sso_profile(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(
        runner.subprocess, "run",
        _fake_aws(caller_fails=True, sso_value="my-sso-session"))
    monkeypatch.setenv("AWS_PROFILE", "tg-sso")
    out = runner.preflight_caller({"AWS_PROFILE": "tg-sso"})
    assert out["ok"] is False
    assert out["is_sso"] is True   # → wizard prints `aws sso login`


def test_preflight_caller_nonsso_failure_not_marked_sso(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_aws(caller_fails=True, sso_value=""))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    out = runner.preflight_caller({})
    assert out["ok"] is False
    assert out["is_sso"] is False  # → generic invalid/expired message


# ── cur-deploy consistency (the live-bug fix, #1087) ──
# tg-cur-deploy.sh must adopt the #768 optional-AWS_PROFILE pattern:
# no hard-require, no per-call --profile "$AWS_PROFILE", and a profile
# preflight that runs before "Validating environment".
_CUR_DEPLOY_SH = _REPO / "scripts" / "tg-cur-deploy.sh"


def test_cur_deploy_no_hard_require_aws_profile():
    src = _CUR_DEPLOY_SH.read_text()
    assert "AWS_PROFILE:?" not in src   # the line-70 hard-require is gone


def test_cur_deploy_uses_profile_args_not_inline_profile():
    src = _CUR_DEPLOY_SH.read_text()
    # no aws call threads the var directly; PROFILE_ARGS carries it.
    assert '--profile "$AWS_PROFILE"' not in src.replace(
        'PROFILE_ARGS=(--profile "$AWS_PROFILE")', "")
    assert 'PROFILE_ARGS=(--profile "$AWS_PROFILE")' in src
    assert '"${PROFILE_ARGS[@]}"' in src


def test_cur_deploy_preflight_runs_before_validate():
    src = _CUR_DEPLOY_SH.read_text()
    # the credential resolution + liveness probe precede env validation.
    assert "Resolving AWS credentials" in src
    assert (src.index("Resolving AWS credentials")
            < src.index("Validating environment"))
    assert "get-caller-identity" in src


def test_main_aborts_before_install_on_preflight_failure():
    # Stop-on-failure invariant (#1087): in __main__.cmd_install the
    # preflight runs, and on failure the function returns BEFORE either
    # install dispatch — so a bad/expired session never starts a deploy.
    main_src = (_REPO / "scripts" / "python" / "tg_cli"
                / "__main__.py").read_text()
    pf = main_src.index("preflight_caller(")
    # the early `return 2` on failure precedes both run dispatches.
    ret = main_src.index("return 2", pf)
    local = main_src.index("run_local_install(", pf)
    ecs = main_src.index("run_install(", pf)
    assert pf < ret < local < ecs
    # and the failure branch surfaces the SSO remediation.
    assert "aws sso login" in main_src[pf:ret]


# ───────────── #1088: build_version from VERSION file + SHA ─────────────
# build_version() derives `v<VERSION>-g<sha>` from the committed VERSION
# file + short HEAD SHA, NOT a `git describe` tag (a force-moved release
# tag that a naive `git pull` leaves stale makes describe fall through to
# a bare SHA on customer clones). Tests mock the git calls + the VERSION
# read — no real git.

class _GitProc:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out


def _fake_git(sha="abc1234", dirty=False, git_ok=True):
    """subprocess.run replacement: `rev-parse --short HEAD` → sha;
    `status --porcelain` → non-empty iff dirty. git_ok=False → all git
    calls fail (rc!=0)."""
    def _run(cmd, *a, **k):
        rc = 0 if git_ok else 128
        if "rev-parse" in cmd:
            return _GitProc(rc, sha if git_ok else "")
        if "status" in cmd:
            return _GitProc(rc, ("M f\n" if (dirty and git_ok) else ""))
        return _GitProc(rc, "")
    return _run


def test_build_version_version_file_plus_sha(monkeypatch, tmp_path):
    import tg_cli.runner as runner
    (tmp_path / "VERSION").write_text("1.1.0\n")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_git(sha="abc1234"))
    assert runner.build_version() == "v1.1.0-gabc1234"


def test_build_version_dirty_tree(monkeypatch, tmp_path):
    import tg_cli.runner as runner
    (tmp_path / "VERSION").write_text("1.1.0")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_git(sha="abc1234", dirty=True))
    assert runner.build_version() == "v1.1.0-gabc1234-dirty"


def test_build_version_no_version_file_falls_back_to_sha(monkeypatch, tmp_path):
    import tg_cli.runner as runner
    # no VERSION file in tmp_path
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_git(sha="abc1234"))
    assert runner.build_version() == "abc1234"


def test_build_version_no_git_no_version_is_dev(monkeypatch, tmp_path):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", _fake_git(git_ok=False))
    assert runner.build_version() == "dev"


def test_build_version_does_not_use_git_describe():
    # The banner's critical path no longer git-describes — assert no
    # `["git", "describe", …]` subprocess call is constructed in the
    # function body (the docstring may still explain WHY we dropped it).
    import inspect
    import tg_cli.runner as runner
    src = inspect.getsource(runner.build_version)
    body = src.split('"""', 2)[-1]   # everything after the docstring
    assert "describe" not in body


# ── installer + publish consistency (#1088) ──
_ECS_INSTALL_SH = _REPO / "scripts" / "tg-ecs-install.sh"
_LOCAL_INSTALL_SH = _REPO / "scripts" / "tg-local-install.sh"
_PUBLISH_SH = _REPO / "internal" / "scripts" / "tg-public-publish.sh"


def test_installers_derive_version_from_version_file_not_describe():
    for sh in (_ECS_INSTALL_SH, _LOCAL_INSTALL_SH):
        src = sh.read_text()
        # the TG_VERSION fallback reads the VERSION file + short SHA...
        assert "/VERSION" in src
        assert "rev-parse --short HEAD" in src
        # ...and no longer INVOKES git describe for TG_VERSION (a comment
        # may still explain WHY it was dropped, so check for the command
        # substitution `$(git describe`, not the bare phrase).
        assert "$(git describe" not in src


def test_publish_allowlist_ships_version_file():
    # WITHOUT this the banner fix is inert on customer clones. Scan the
    # whole array — the closing `)` of a comment like build_version()
    # must NOT be mistaken for the array's terminator, so look for a
    # bare `VERSION` entry on its own line anywhere after the opener.
    src = _PUBLISH_SH.read_text()
    al = src.index("TG_PUBLISH_ALLOW_PATHS=(")
    block = src[al:al + 4000]
    assert any(line.strip() == "VERSION" for line in block.splitlines())


# ───────────── #1093: abort on set-but-not-found AWS_PROFILE ─────────────
# A SET-but-unresolvable AWS_PROFILE (a typo) must abort up front naming
# the bad profile — not silently prompt for the account. profile_not_found
# uses `aws configure list-profiles` (mocked here); distinct from #1087's
# expired-session case (that profile DOES exist).

def _fake_list_profiles(names, rc=0):
    """subprocess.run replacement for `aws configure list-profiles`."""
    class _P:
        def __init__(self):
            self.returncode = rc
            self.stdout = "\n".join(names) + ("\n" if names else "")
    def _run(cmd, *a, **k):
        return _P()
    return _run


def test_profile_not_found_true_for_typo(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_list_profiles(["demo0-tg-install", "default"]))
    assert runner.profile_not_found("demo0-tg-intall") is True   # typo


def test_profile_not_found_false_for_valid(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_list_profiles(["demo0-tg-install", "default"]))
    assert runner.profile_not_found("demo0-tg-install") is False


def test_profile_not_found_false_when_unset(monkeypatch):
    import tg_cli.runner as runner
    # unset profile is the valid default-chain path — never not-found.
    assert runner.profile_not_found(None) is False
    assert runner.profile_not_found("") is False


def test_profile_not_found_fails_open_when_cannot_list(monkeypatch):
    import tg_cli.runner as runner
    # can't enumerate (rc!=0 / empty) → don't claim not-found (let the
    # downstream liveness probe report it instead of a false abort).
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_list_profiles([], rc=1))
    assert runner.profile_not_found("anything") is False
    monkeypatch.setattr(runner.subprocess, "run",
                        _fake_list_profiles([], rc=0))
    assert runner.profile_not_found("anything") is False


def test_cmd_install_aborts_before_account_q_on_bad_profile():
    # Ordering invariant (#1093): in __main__.cmd_install the
    # profile_not_found abort runs BEFORE the account question, so a typo'd
    # profile never silently prompts for the account.
    full = (_REPO / "scripts" / "python" / "tg_cli"
            / "__main__.py").read_text()
    # Scope to cmd_install's body (runs to the next top-level `def `),
    # so a same-named reference elsewhere can't skew the offsets.
    start = full.index("def cmd_install(")
    nxt = full.index("\ndef ", start + 1)
    main_src = full[start:nxt]
    pnf = main_src.index("profile_not_found(")
    acct_q = main_src.index('key="account_id"')
    assert pnf < acct_q
    # the abort returns before reaching the account question.
    ret = main_src.index("return 2", pnf)
    assert pnf < ret < acct_q
    # and the message names the bad profile.
    assert "could not be found" in main_src[pnf:ret]


# ───────────── #1104: display_version (bare release for displays) ─────────────
# display_version reduces a full build version to v1.1.0 for the banner
# + UI footer; /api/version + the deploy stamp keep the FULL string.

def test_display_version_collapses_release():
    import tg_cli.runner as runner
    assert runner.display_version("v1.1.0-ga2c3a69-dirty") == "v1.1.0"
    assert runner.display_version("v1.1.0-ga2c3a69") == "v1.1.0"
    assert runner.display_version("v1.1.0") == "v1.1.0"


def test_display_version_passes_through_non_release():
    import tg_cli.runner as runner
    # bare short SHA (no v-prefix) — no release to collapse to; keep it.
    assert runner.display_version("a2c3a69") == "a2c3a69"
    assert runner.display_version("a2c3a69-dirty") == "a2c3a69-dirty"
    # the literal dev fallback passes through.
    assert runner.display_version("dev") == "dev"


def test_display_version_does_not_alter_build_version_thread():
    # The banner reduces, but build_version()'s OWN return (what feeds
    # TG_VERSION / the deploy stamp) is untouched — confirm the two
    # functions are distinct and build_version still yields the full
    # v<ver>-g<sha> shape on a normal checkout.
    import tg_cli.runner as runner
    # __main__ threads build_ver (full) to TG_VERSION, not the reduced
    # form — assert the source still wires the FULL value.
    main_src = (_REPO / "scripts" / "python" / "tg_cli"
                / "__main__.py").read_text()
    assert 'env["TG_VERSION"] = build_ver' in main_src   # full, not reduced
    assert "display_version(build_ver)" in main_src       # banner reduces


# ── #1115: Python min-version gate + Bash-info line ──────────────────

def test_min_python_is_single_source_constant():
    # The floor is a named constant the gate reads — no hardcoded
    # duplicate. (No pyproject.toml in this repo; the constant IS the
    # source of truth.) Empirically determined to 3.9 (see runner.py).
    import tg_cli.runner as runner
    assert runner.TG_MIN_PYTHON == (3, 9)


def test_check_python_below_min_fails(monkeypatch):
    import tg_cli.runner as runner
    # The PATH python3 the bash scripts would use reports 3.7 → too old.
    monkeypatch.setattr(runner, "_path_python3_version", lambda: (3, 7))
    ok, detected = runner.check_python()
    assert ok is False
    assert detected == "3.7"


def test_check_python_at_min_passes(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "_path_python3_version", lambda: (3, 9))
    ok, detected = runner.check_python()
    assert ok is True
    assert detected == "3.9"


def test_check_python_above_min_passes(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "_path_python3_version", lambda: (3, 12))
    ok, _ = runner.check_python()
    assert ok is True


def test_check_python_checks_path_python3_not_cli_interpreter(monkeypatch):
    # The gate must test the python3 the SCRIPTS invoke, not this
    # process's sys.version_info. With PATH python3 reporting OLD while
    # the CLI interpreter (always >=3.9 in CI) is new, the gate FAILS —
    # proving it reads the PATH probe, not sys.
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "_path_python3_version", lambda: (3, 6))
    ok, detected = runner.check_python()
    assert ok is False and detected == "3.6"


def test_check_python_falls_back_to_sys_when_path_probe_fails(monkeypatch):
    # If PATH python3 can't be probed, fall back to this interpreter's
    # version (CI runs >=3.9, so this passes) — never a spurious abort.
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "_path_python3_version", lambda: None)
    ok, _ = runner.check_python()
    assert ok is True   # CI interpreter is >= the floor


def test_python_upgrade_message_names_versions_and_both_platforms():
    import tg_cli.runner as runner
    msg = runner.python_upgrade_message("3.7")
    assert "3.9+" in msg and "3.7" in msg          # required + detected
    assert "brew install python@3.9" in msg        # macOS (Homebrew)
    assert "python.org" in msg                     # macOS (installer)
    assert "Linux" in msg                          # distro hint


def test_install_gates_python_before_run_install():
    # The gate is wired into cmd_install BEFORE run_install, and applies
    # even to --dry-run (the wizard runs on this interpreter). Source
    # assertion (mirrors the #1104 thread-check style).
    main_src = (_REPO / "scripts" / "python" / "tg_cli"
                / "__main__.py").read_text()
    gate = main_src.index("runner.check_python()")
    run = main_src.index("runner.run_install(")
    assert gate < run, "check_python must gate before run_install"
    # aborts on failure (returns before the install)
    assert "python_upgrade_message" in main_src


def test_bash_version_is_informational_not_a_gate():
    # The installer PRINTS the Bash version but never aborts on it —
    # the #1105/#1112 compatibility stance. No `fail`/`exit` tied to the
    # BASH_VERSINFO check; only an `ok`/echo + a soft note.
    sh = (_REPO / "scripts" / "tg-ecs-install.sh").read_text()
    assert 'Using Bash ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}' in sh
    # the version branch is a soft note, not an abort
    i = sh.index('"${BASH_VERSINFO[0]}" -lt 4')
    window = sh[i:i + 260]
    assert "fail " not in window and "exit 1" not in window
    assert "report it" in window


# ── #1119: wizard owns Done banner + hoisted CUR decision ────────────

def test_ecs_summary_emitted_not_printed_under_wizard():
    # Under the wizard (TG_SUMMARY_OUT set) the ECS script writes the
    # summary VALUES and does NOT print the == Done == block; standalone
    # (var unset) it still prints. Source assertion on the guard.
    sh = (_REPO / "scripts" / "tg-ecs-install.sh").read_text()
    assert 'if [[ -n "${TG_SUMMARY_OUT:-}" ]]; then' in sh
    # the printed summary is the ELSE (standalone) branch — still present
    assert "Install complete" in sh
    # and the KEY=VALUE handoff writes the fields the banner needs
    for key in ("API_HOST", "SIGNIN_AS", "LOGIN_PROVIDER"):
        assert f'echo "{key}=' in sh


def test_cur_deploy_honors_decision_flag():
    # tg-cur-deploy.sh skips the interactive read when TG_CUR_DECISION is
    # set; keeps the [[ -t 0 ]] interactive + safe-default for standalone.
    sh = (_REPO / "scripts" / "tg-cur-deploy.sh").read_text()
    assert '"${TG_CUR_DECISION:-}" == "reuse"' in sh
    assert '"${TG_CUR_DECISION:-}" == "create"' in sh
    # the interactive fallback survives for standalone runs
    assert "read -r -p" in sh and "[[ -t 0 ]]" in sh


def test_render_done_banner_content():
    import tg_cli.runner as runner
    summary = {
        "API_SCHEME": "https", "API_HOST": "tg-alb-123.elb.amazonaws.com",
        "SIGNIN_AS": "admin@example.com", "LOGIN_PROVIDER": "Cognito",
        "AWS_REGION": "us-east-1", "PROFILE_HINT": "",
    }
    out = runner.render_done_banner(summary, "Cost reporting: configured")
    assert "Install complete" in out
    assert "https://tg-alb-123.elb.amazonaws.com/" in out
    assert "admin@example.com" in out
    assert "Cognito login" in out
    assert "Cost reporting: configured" in out      # CUR line folded in
    # Cognito creds hint present
    assert "Forgot password" in out


def test_render_done_banner_okta_omits_cognito_creds():
    import tg_cli.runner as runner
    summary = {"API_SCHEME": "https", "API_HOST": "h", "SIGNIN_AS": "a@b.c",
               "LOGIN_PROVIDER": "Okta", "AWS_REGION": "us-east-1",
               "PROFILE_HINT": ""}
    out = runner.render_done_banner(summary, None)
    assert "Okta login" in out
    assert "Forgot password" not in out   # Cognito-only hint


def test_cur_reuse_candidate_none_on_no_exports(monkeypatch):
    import tg_cli.runner as runner
    import subprocess as _sp
    class _P:
        returncode = 0
        stdout = '{"Exports": []}'
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P())
    assert runner.cur_reuse_candidate("us-east-1", None) is None


def test_cur_reuse_candidate_finds_bedrock_export(monkeypatch):
    import tg_cli.runner as runner
    import subprocess as _sp
    class _P:
        returncode = 0
        stdout = '{"Exports": [{"ExportName": "tg-bedrock-cur"}]}'
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P())
    assert runner.cur_reuse_candidate("us-east-1", None) == "tg-bedrock-cur"


def test_cur_reuse_candidate_none_on_probe_error(monkeypatch):
    import tg_cli.runner as runner
    import subprocess as _sp
    def boom(*a, **k):
        raise OSError("no aws")
    monkeypatch.setattr(_sp, "run", boom)
    assert runner.cur_reuse_candidate("us-east-1", None) is None


def test_ask_cur_decision_preset_wins(monkeypatch):
    import tg_cli.__main__ as M
    monkeypatch.setenv("TG_CUR_DECISION", "reuse")
    assert M._ask_cur_decision({"region": "us-east-1"}) == "reuse"


def test_ask_cur_decision_none_when_no_candidate(monkeypatch):
    import tg_cli.__main__ as M
    import tg_cli.runner as runner
    monkeypatch.delenv("TG_CUR_DECISION", raising=False)
    monkeypatch.setattr(runner, "cur_reuse_candidate", lambda *a, **k: None)
    assert M._ask_cur_decision({"region": "us-east-1"}) is None


def test_ask_cur_decision_asks_when_candidate_and_tty(monkeypatch):
    import tg_cli.__main__ as M
    import tg_cli.runner as runner
    monkeypatch.delenv("TG_CUR_DECISION", raising=False)
    monkeypatch.setattr(runner, "cur_reuse_candidate",
                        lambda *a, **k: "tg-bedrock-cur")
    monkeypatch.setattr(M.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")   # default → reuse
    assert M._ask_cur_decision({"region": "us-east-1"}) == "reuse"
    monkeypatch.setattr("builtins.input", lambda *a: "c")
    assert M._ask_cur_decision({"region": "us-east-1"}) == "create"


def test_ask_cur_decision_no_autoattach_when_candidate_but_no_tty(monkeypatch):
    # A candidate exists but stdin isn't a TTY → don't auto-attach
    # unattended; return None so the script's safe create-default applies.
    import tg_cli.__main__ as M
    import tg_cli.runner as runner
    monkeypatch.delenv("TG_CUR_DECISION", raising=False)
    monkeypatch.setattr(runner, "cur_reuse_candidate",
                        lambda *a, **k: "tg-bedrock-cur")
    monkeypatch.setattr(M.sys.stdin, "isatty", lambda: False)
    assert M._ask_cur_decision({"region": "us-east-1"}) is None


def test_run_install_returns_tuple_and_threads_summary_out(monkeypatch):
    # run_install passes TG_SUMMARY_OUT and returns (rc, parsed-summary).
    import tg_cli.runner as runner
    import subprocess as _sp
    captured = {}
    def fake_call(cmd, env=None):
        captured["env"] = env
        # simulate the script writing the handoff file
        Path(env["TG_SUMMARY_OUT"]).write_text(
            "API_HOST=h\nLOGIN_PROVIDER=Cognito\n")
        return 0
    monkeypatch.setattr(_sp, "call", fake_call)
    rc, summary = runner.run_install({"TG_FOO": "1"})
    assert rc == 0
    assert "TG_SUMMARY_OUT" in captured["env"]
    assert summary["API_HOST"] == "h"
    assert summary["LOGIN_PROVIDER"] == "Cognito"


def test_done_banner_is_wizard_owned_not_ecs_substep():
    # Structural: the wizard prints render_done_banner AFTER _run_addons
    # (CUR), so "Done" can't precede CUR. Source ordering assertion.
    main_src = (_REPO / "scripts" / "python" / "tg_cli"
                / "__main__.py").read_text()
    addons = main_src.index("_run_addons(args, env)")
    banner = main_src.index("render_done_banner(")
    assert addons < banner, "Done banner must be printed after CUR addons"


# ── #1123: lockstep image<->CFN-template versioning ──────────────────

def test_semver_tuple_ignores_v_prefix_and_suffix():
    import tg_cli.runner as runner
    assert runner._semver_tuple("v1.1.0-ga2c3a69") == (1, 1, 0)
    assert runner._semver_tuple("1.1.0") == (1, 1, 0)
    assert runner._semver_tuple("v1.2.3-dirty") == (1, 2, 3)
    assert runner._semver_tuple("dev") is None
    assert runner._semver_tuple("") is None


def test_template_min_image_version_reads_marker():
    # The real CUR template carries Metadata.TgMinImageVersion = 1.1.0
    # (the {{DATE_FILTER}} requirement).
    import tg_cli.runner as runner
    assert runner.template_min_image_version(
        runner.CUR_DEPLOY_TEMPLATE) == "1.1.0"


def test_template_min_image_version_absent_returns_none(tmp_path):
    import tg_cli.runner as runner
    p = tmp_path / "t.yaml"
    p.write_text("Resources:\n  Foo:\n    Type: AWS::S3::Bucket\n")
    assert runner.template_min_image_version(p) is None


def test_compat_old_image_is_skew(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_label_version",
                        lambda ref, timeout=4.0: "1.0.0")
    status, msg = runner.check_image_template_compat(
        "public.ecr.aws/x/tg-container:old", runner.CUR_DEPLOY_TEMPLATE)
    assert status == "skew"
    assert "1.0.0" in msg and "1.1.0" in msg


def test_compat_exact_and_newer_image_ok(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_label_version",
                        lambda ref, timeout=4.0: "1.1.0")
    assert runner.check_image_template_compat(
        "r", runner.CUR_DEPLOY_TEMPLATE)[0] == "ok"
    monkeypatch.setattr(runner, "image_label_version",
                        lambda ref, timeout=4.0: "1.2.0")
    assert runner.check_image_template_compat(
        "r", runner.CUR_DEPLOY_TEMPLATE)[0] == "ok"


def test_compat_unreadable_label_warns_not_refuses(monkeypatch):
    # A label we can't read (pre-LABEL image / digest ref / offline) →
    # WARN, never a hard skew-refuse (don't block a deploy on an
    # unreadable label).
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_label_version",
                        lambda ref, timeout=4.0: None)
    status, msg = runner.check_image_template_compat(
        "public.ecr.aws/x/tg-container:x", runner.CUR_DEPLOY_TEMPLATE)
    assert status == "warn"
    assert "1.1.0" in msg


def test_compat_no_marker_is_ok(monkeypatch, tmp_path):
    # A template with no TgMinImageVersion → nothing to enforce → ok,
    # even if the label is unreadable.
    import tg_cli.runner as runner
    p = tmp_path / "t.yaml"
    p.write_text("Resources: {}\n")
    monkeypatch.setattr(runner, "image_label_version",
                        lambda ref, timeout=4.0: None)
    assert runner.check_image_template_compat("r", p)[0] == "ok"


def test_image_label_version_none_on_non_public_ref():
    # A private/digest ref has no public tag to inspect → None (→ warn).
    import tg_cli.runner as runner
    assert runner.image_label_version(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/x:y") is None
    assert runner.image_label_version(
        "public.ecr.aws/a/b@sha256:abc") is None


def test_install_gates_image_template_compat_before_run_install():
    # cmd_install runs the compat check (skew → return before
    # run_install). Source assertion: the check + a skew return precede
    # run_install.
    main_src = (_REPO / "scripts" / "python" / "tg_cli"
                / "__main__.py").read_text()
    chk = main_src.index("check_image_template_compat(")
    run = main_src.index("runner.run_install(")
    assert chk < run, "compat gate must run before run_install"
    # only the prebuilt-image path is gated (build-from-source is fresh)
    assert "TG_ECS_IMAGE_URI" in main_src[chk - 400:chk]
    # skew aborts; warn does not
    assert 'if _status == "skew"' in main_src


def test_dockerfile_has_version_label():
    df = (_REPO / "container" / "Dockerfile").read_text()
    assert 'LABEL org.tg.version="${TG_VERSION}"' in df


def test_cur_template_has_min_image_marker():
    tmpl = (_REPO / "cfn" / "tg-cur-athena.yaml").read_text()
    assert "Metadata:" in tmpl
    assert "TgMinImageVersion: '1.1.0'" in tmpl


# ── #1130: AWS identity report moved to the top of tg install ────────

_MAIN_SRC = (_REPO / "scripts" / "python" / "tg_cli"
             / "__main__.py").read_text()


def test_identity_report_prints_before_build_version():
    # #1130: the "Using AWS credentials / Logged in as" identity report
    # must print BEFORE the build-version line (owner confirms account
    # up front).
    pf = _MAIN_SRC.index("runner.preflight_caller(")
    bv = _MAIN_SRC.index("tg build version:")
    assert pf < bv, "identity report must precede the build-version line"


def test_identity_report_prints_before_account_question():
    # The relocated preflight sits right after _seed_answers and BEFORE
    # run_install (the wizard account question runs between them). Proxy:
    # preflight precedes run_install and sits in the first half of
    # cmd_install, not after the Q&A.
    pf = _MAIN_SRC.index("runner.preflight_caller(")
    run = _MAIN_SRC.index("runner.run_install(")
    seed = _MAIN_SRC.index("_seed_answers(args.non_interactive")
    assert seed < pf < run, "preflight must run after seed, before deploy"


def test_identity_report_prints_exactly_once():
    # No double-print: the old post-Q&A call site was removed.
    assert _MAIN_SRC.count("runner.preflight_caller(") == 1
    assert _MAIN_SRC.count("Logged in as:") == 1


def test_creds_abort_is_before_run_install():
    # ok==False aborts up front (return 2) — before run_install. The
    # abort lives in the same top block as the preflight call.
    pf = _MAIN_SRC.index("runner.preflight_caller(")
    # the abort branch + its return 2 follow the call (before deploy)
    err = _MAIN_SRC.index("could not verify AWS credentials", pf)
    ret = _MAIN_SRC.index("return 2", err)   # the abort's return
    run = _MAIN_SRC.index("runner.run_install(")
    assert pf < err < ret < run


def test_relocated_preflight_documents_profile_caveat():
    # The placement-fragility caveat (move it if profile becomes a wizard
    # question) is recorded so the early placement isn't naively rebroken.
    assert "becomes a wizard\n    # question" in _MAIN_SRC or \
           "BECOMES a wizard" in _MAIN_SRC


# ── image<->repo sync advisory hint (wizard Part 2) ──────────────────
# newer_public_image_hint is STRICTLY fail-silent: a hint only when the
# channel tag (latest) resolves to a DIFFERENT digest than the pinned
# version, and only when BOTH digests + a pinned version resolve.

def test_hint_when_latest_differs_from_pinned(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_version", lambda: "v1.1.0")
    digs = {"latest": "sha256:newAAA", "v1.1.0": "sha256:oldBBB"}
    monkeypatch.setattr(runner, "public_image_digest",
                        lambda tag, timeout=4.0: digs.get(tag))
    hint = runner.newer_public_image_hint()
    assert hint is not None and "newer public image" in hint


def test_no_hint_when_digests_equal(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_version", lambda: "v1.1.0")
    monkeypatch.setattr(runner, "public_image_digest",
                        lambda tag, timeout=4.0: "sha256:same")
    assert runner.newer_public_image_hint() is None


def test_no_hint_when_a_digest_unresolvable(monkeypatch):
    # Offline / 404 on either tag → can't tell → silent (None).
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_version", lambda: "v1.1.0")
    monkeypatch.setattr(runner, "public_image_digest",
                        lambda tag, timeout=4.0:
                        "sha256:x" if tag == "latest" else None)
    assert runner.newer_public_image_hint() is None


def test_no_hint_when_no_pinned_version(monkeypatch):
    # No version-pin for this checkout (or it IS the channel tag) → no
    # comparison to make → silent.
    import tg_cli.runner as runner
    monkeypatch.setattr(runner, "image_version", lambda: None)
    monkeypatch.setattr(runner, "public_image_digest",
                        lambda tag, timeout=4.0: "sha256:whatever")
    assert runner.newer_public_image_hint() is None
    monkeypatch.setattr(runner, "image_version",
                        lambda: runner.PUBLIC_ECR_CHANNEL_TAG)
    assert runner.newer_public_image_hint() is None


def test_wizard_guards_the_hint_against_any_probe_error():
    # The advisory must NEVER block the install or surface an error. The
    # wizard wraps newer_public_image_hint in a try/except that swallows
    # anything and prints only a non-None hint. Assert that guard exists
    # and the hint is in the greenfield branch (after resolve_prebuilt_
    # image), never gating the choice.
    import tg_cli.wizard as wizard
    src = Path(wizard.__file__).read_text()
    assert "newer_public_image_hint" in src
    assert "advisory must never raise" in src
    i_resolve = src.index("resolve_prebuilt_image()")
    i_hint = src.index("newer_public_image_hint")
    assert i_resolve < i_hint


# ── confirm-gate abort must NOT be ignored as a cosmetic glitch ───────
# A re-run where tg-container-stack already exists + is healthy: pressing
# Enter (no y) at the account-confirm gate aborts the bash installer
# (reserved exit code), but the #1067 "core healthy → ignore non-zero,
# continue to CUR" guard used to swallow it (health was True from the
# PRIOR install) and proceed to CUR — overriding the operator's "no".
# Fixed by classifying on the EXIT CODE, not stack health alone.

def test_ignore_cosmetic_nonzero_screens_out_the_abort_code():
    import tg_cli.__main__ as m
    # the abort / pre-deploy-fail code is NEVER ignorable (fatal)
    assert m._ignore_cosmetic_nonzero(m.INSTALLER_ABORT_EXIT) is False
    # a clean exit is not a non-zero to consider
    assert m._ignore_cosmetic_nonzero(0) is False
    # a generic non-zero (the cosmetic summary glitch) IS a candidate
    # (the caller then also requires a healthy stack)
    assert m._ignore_cosmetic_nonzero(1) is True
    assert m._ignore_cosmetic_nonzero(2) is True


def test_abort_exit_code_matches_installer_contract():
    # The wizard's reserved code MUST equal the bash installer's
    # TG_ABORT_EXIT (the cross-file contract); a drift would re-open the
    # bug (the wizard would treat the abort as a cosmetic glitch again).
    import tg_cli.__main__ as m
    assert m.INSTALLER_ABORT_EXIT == 3
    inst = (_REPO / "scripts" / "tg-ecs-install.sh").read_text()
    assert "TG_ABORT_EXIT=3" in inst
    # fail() must exit with that reserved code, not a bare exit 1.
    assert 'exit "$TG_ABORT_EXIT"' in inst


def test_install_guard_uses_exit_code_not_health_alone():
    # Structural: the ignore-and-continue branch must go through the
    # exit-code predicate (so an abort can't be ignored on a healthy
    # re-run), and the abort exit must have its own fatal branch.
    assert "_ignore_cosmetic_nonzero(rc) and _core_stack_healthy" in _MAIN_SRC
    assert "elif rc == INSTALLER_ABORT_EXIT:" in _MAIN_SRC
    # the predicate excludes the abort code
    assert "rc != 0 and rc != INSTALLER_ABORT_EXIT" in _MAIN_SRC


# ── Docker build-capability preflight ────────────────────────
# build-from-source needs a Docker that can BUILD, not just answer
# `docker info`. Docker Desktop's org-sign-in/policy block lets the
# daemon respond while `docker build` fails — the install used to die
# mid-build. These pin the error classifier, the build-smoke probe
# (subprocess mocked), and the cmd_install gate placement.

def test_classify_docker_error_signin():
    import tg_cli.runner as runner
    for msg in (
        "ERROR: failed to build: Error response from daemon: Sign in to "
        "continue using Docker Desktop.",
        "Membership in the [amazonians] organization is required.",
        "Sign-in enforced by your administrators (via Config Profile).",
    ):
        assert runner._classify_docker_error(msg) == "signin"


def test_classify_docker_error_daemon_and_missing():
    import tg_cli.runner as runner
    assert runner._classify_docker_error(
        "Cannot connect to the Docker daemon ... Is the docker daemon "
        "running?") == "daemon_down"
    assert runner._classify_docker_error(
        "docker: command not found") == "no_docker"
    assert runner._classify_docker_error(
        "some other build error") == "build_failed"


def test_docker_fix_message_always_offers_prebuilt():
    import tg_cli.runner as runner
    for cause in ("signin", "no_docker", "daemon_down", "build_failed"):
        m = runner._docker_fix_message(cause)
        assert "prebuilt public image" in m
    # the sign-in case names the org-policy + sign-in remedy
    assert "sign in" in runner._docker_fix_message("signin").lower()


def test_docker_build_preflight_ok_when_build_succeeds(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.shutil, "which", lambda _x: "/usr/bin/docker")

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: _P())
    ok, msg = runner.docker_build_preflight()
    assert ok is True and msg == ""


def test_docker_build_preflight_signin_block_aborts(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.shutil, "which", lambda _x: "/usr/bin/docker")

    class _P:
        returncode = 1
        stdout = ""
        stderr = ("ERROR: failed to build: Sign in to continue using "
                  "Docker Desktop. Membership in the [org] organization "
                  "is required.")
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: _P())
    ok, msg = runner.docker_build_preflight()
    assert ok is False
    assert "sign in" in msg.lower()
    assert "prebuilt public image" in msg


def test_docker_build_preflight_no_docker(monkeypatch):
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.shutil, "which", lambda _x: None)
    ok, msg = runner.docker_build_preflight()
    assert ok is False
    assert "not installed" in msg.lower() or "not on path" in msg.lower()


def test_docker_build_preflight_fails_closed_on_spawn_error(monkeypatch):
    # A timeout / OSError spawning docker → fail closed (never proceed to
    # a real build we couldn't validate).
    import tg_cli.runner as runner
    monkeypatch.setattr(runner.shutil, "which", lambda _x: "/usr/bin/docker")

    def _boom(*a, **k):
        raise OSError("spawn failed")
    monkeypatch.setattr(runner.subprocess, "run", _boom)
    ok, _msg = runner.docker_build_preflight()
    assert ok is False


def test_cmd_install_gates_docker_preflight_on_build_only():
    # Structural: the preflight runs ONLY when image == "build" (the
    # prebuilt path needs no Docker), after the dry-run return, before
    # any deploy. And the bash backstop is build-gated too.
    assert 'answers.get("image") == "build"' in _MAIN_SRC
    assert "docker_build_preflight()" in _MAIN_SRC
    inst = (_REPO / "scripts" / "tg-ecs-install.sh").read_text()
    assert 'if [[ -z "${TG_ECS_IMAGE_URI:-}" ]]; then' in inst
    assert "docker buildx build --output type=cacheonly" in inst


def test_real_build_pins_amd64_platform_and_asserts_arch():
    # The real image build MUST carry --platform linux/amd64 (the task
    # def has no runtimePlatform → Fargate defaults x86; an arm64-built
    # image crash-loops with "exec format error"), and the build must
    # fail loud if the produced image isn't amd64 (the bug was a SILENT
    # arch mismatch only seen as an ECS crash loop).
    inst = (_REPO / "scripts" / "tg-ecs-install.sh").read_text()
    # the REAL build line (distinct from the cacheonly preflight probe)
    assert "--platform linux/amd64" in inst
    # tie the flag to the real build: it precedes the --build-arg
    # TG_VERSION line, which is unique to the real build (the cacheonly
    # preflight probe has no --build-arg).
    build_idx = inst.index('--build-arg "TG_VERSION=$TG_VERSION"')
    window = inst[build_idx - 200:build_idx]
    assert "--platform linux/amd64" in window
    # post-build arch assertion fails loud on non-amd64
    assert "{{.Architecture}}" in inst
    assert '"$built_arch" != "amd64"' in inst
# ── --verbose gates the Advanced/troubleshooting block + live-logs
#    spacing (the success banner).
_BANNER_SUMMARY = {
    "API_SCHEME": "https", "API_HOST": "tg-alb-1.elb.amazonaws.com",
    "SIGNIN_AS": "admin@example.com", "LOGIN_PROVIDER": "Cognito",
    "AWS_REGION": "us-east-1", "PROFILE_HINT": "--profile demo0-tg-install ",
}


def test_done_banner_hides_advanced_block_by_default():
    import tg_cli.runner as runner
    out = runner.render_done_banner(_BANNER_SUMMARY, "Cost reporting: ok")
    # always-on lines stay
    assert "Install complete" in out
    assert "Cost reporting: ok" in out
    # advanced block is hidden by default
    assert "Advanced / troubleshooting" not in out
    assert "Health check" not in out
    assert "Live logs" not in out
    assert "Tear down" not in out


def test_done_banner_shows_advanced_block_when_verbose():
    import tg_cli.runner as runner
    out = runner.render_done_banner(
        _BANNER_SUMMARY, "Cost reporting: ok", verbose=True)
    assert "Advanced / troubleshooting" in out
    assert "Health check" in out
    assert "Live logs" in out
    assert "Tear down" in out


def test_live_logs_spacing_with_profile():
    # The bug: PROFILE_HINT's trailing space dropped on the KEY=VALUE
    # round-trip → `install--region`. The line must have a SPACE between
    # --profile <p> and --region <r>, copy-paste-valid.
    import tg_cli.runner as runner
    out = runner.render_done_banner(_BANNER_SUMMARY, None, verbose=True)
    assert ("aws logs tail /ecs/tg-container --follow "
            "--profile demo0-tg-install --region us-east-1") in out
    assert "install--region" not in out   # the bug
    assert "demo0-tg-install  --region" not in out  # no double space


def test_live_logs_no_profile_no_dangling_flag():
    # With no profile, the command is `… --follow --region <r>` — no
    # stray --profile, no double/leading space.
    import tg_cli.runner as runner
    s = dict(_BANNER_SUMMARY, PROFILE_HINT="")
    out = runner.render_done_banner(s, None, verbose=True)
    assert ("aws logs tail /ecs/tg-container --follow "
            "--region us-east-1") in out
    assert "--profile" not in out
    assert "--follow  --region" not in out  # no double space


def test_live_logs_cmd_helper_units():
    import tg_cli.runner as runner
    assert runner._live_logs_cmd("--profile p ", "us-east-1") == \
        "aws logs tail /ecs/tg-container --follow --profile p --region us-east-1"
    assert runner._live_logs_cmd("", "us-east-1") == \
        "aws logs tail /ecs/tg-container --follow --region us-east-1"


def test_install_has_verbose_flag_and_threads_it():
    # Structural: the install parser has --verbose/-v and the banner
    # render is passed verbose=; the bash path gates on TG_VERBOSE.
    assert '"--verbose", "-v"' in _MAIN_SRC
    assert "verbose=getattr(args, \"verbose\", False)" in _MAIN_SRC
    inst = (_REPO / "scripts" / "tg-ecs-install.sh").read_text()
    assert 'if [[ "${TG_VERBOSE:-}" == "1" ]]; then' in inst


# ── keep config-<account>.json on success; --full-reset still wipes
def test_config_clear_removes_account_keyed_file(tmp_path, monkeypatch):
    # The wipe primitive --full-reset uses: clear(acct) unlinks exactly
    # that account's file and is a no-op when absent.
    monkeypatch.setenv("TG_CONFIG_HOME", str(tmp_path))
    import importlib
    importlib.reload(config)
    config.save({"account_id": "123456789012", "region": "us-east-1"},
                "123456789012")
    p = tmp_path / "config-123456789012.json"
    assert p.exists()
    config.clear("123456789012")
    assert not p.exists()
    config.clear("123456789012")   # idempotent — no error when absent
    monkeypatch.delenv("TG_CONFIG_HOME", raising=False)
    importlib.reload(config)


def test_install_keeps_config_on_success_not_cleared():
    # The success path must NOT delete the account-keyed config:
    # no `config.clear(...)` may sit under an `if rc == 0:`. (A re-install
    # then pre-fills the saved answers via _seed_answers.)
    src = _MAIN_SRC
    # there is no longer a success-gated clear: the only clear() is the
    # full-reset-at-START one.
    assert src.count("config.clear(") == 1, \
        "expected exactly one config.clear (the --full-reset wipe)"
    # and it is NOT under a success branch — it sits in the full-reset
    # start block.
    i_clear = src.index("config.clear(")
    # the nearest preceding control line should be the full-reset elif,
    # not `if rc == 0:`
    head = src[:i_clear]
    assert head.rfind("elif acct and args.full_reset:") > head.rfind("if rc == 0:"), \
        "config.clear must be in the --full-reset block, not a success branch"


def test_full_reset_wipes_config_at_start():
    # --full-reset clears the account-keyed file at START (account known),
    # unconditional of the run outcome.
    src = _MAIN_SRC
    assert "elif acct and args.full_reset:" in src
    # the clear is inside that branch
    i_branch = src.index("elif acct and args.full_reset:")
    # window large enough to span the explanatory comment + the call
    after = src[i_branch:i_branch + 1200]
    assert "config.clear(acct)" in after
