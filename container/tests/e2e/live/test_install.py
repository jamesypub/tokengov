"""Live-tier e2e for the REAL installer (`scripts/tg install`).

This is the highest-fidelity coverage of `tg install`: it drives the
actual bash launcher (`scripts/tg` → `python3 -m tg_cli`) against a
live demo2-class target and asserts the installer's OWN observable
checkpoints — the preflight identity report, the confirm screen, the
CFN terminal state, the deployed `/api/version`, and the single "Done"
banner (#1119) — for two paths:

  1. UPGRADE (default, non-destructive): re-run `tg install` over a
     live tg-container-stack, assert it takes the in-place UPGRADE path
     (#962) and that pre-seeded data survives a rolling deploy.
  2. NEW clean-room (operator-only, behind E2E_INSTALL_DESTROY=1):
     tear demo2 down first, then a fresh `tg install`, assert the
     NEW-install checkpoints.

Everything is env-configured (E2E_* / TG_* seeded answers); NOTHING
that identifies a target (account id, profile, image) is hardcoded as
the only value. The whole tier SKIPS cleanly and credential-free when
E2E_API_BASE is unset or the stack is unreachable (via the `live_base`
fixture), so a bare `pytest -m live` with no env skips and
`pytest -m "not live"` is unaffected. No product code — test-only.

The installer's checkpoint strings asserted here are lifted verbatim
from scripts/python/tg_cli/runner.py (render_confirm / render_done_
banner) and __main__.py (the preflight identity report) — kept in
lockstep with those surfaces, not paraphrased.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

from tests.e2e.live.conftest import SEED_EMAIL

pytestmark = pytest.mark.live

# ── config (os.environ; no hardcoded target values as the only value) ─

CONTAINER_STACK = "tg-container-stack"
# SEED_EMAIL comes from conftest (E2E_SEED_EMAIL, reserved @example.com)
# — the SAME reserved principal the workflow/enforcement live tests use.
# A generous ceiling: a real ECS/RDS/ALB create + CUR deploy is slow.
INSTALL_TIMEOUT = int(os.environ.get("E2E_INSTALL_TIMEOUT", "3600") or "3600")
DESTROY_TIMEOUT = int(os.environ.get("E2E_DESTROY_TIMEOUT", "3600") or "3600")

# Confirm-screen mode lines (render_confirm): whitespace-tolerant so a
# padding change to the label column doesn't break the assertion.
_MODE_UPGRADE_RE = re.compile(
    r"Mode\s*:\s*UPGRADE existing tg-container-stack")
_MODE_NEW_RE = re.compile(r"Mode\s*:\s*NEW install")
# The image from→to line (only present on upgrade when from != to).
_IMAGE_ARROW_RE = re.compile(r"Image\s*:\s*.+→.+")


def _repo_root() -> pathlib.Path:
    """Repo root, resolved from THIS file's location (never hardcoded).

    tests/e2e/live/test_install.py → parents[4] is the repo root; verify
    by the presence of scripts/tg (the launcher we invoke)."""
    return pathlib.Path(__file__).resolve().parents[4]


def _tg_launcher() -> pathlib.Path:
    return _repo_root() / "scripts" / "tg"


def _require_launcher() -> pathlib.Path:
    """The scripts/tg launcher path, or a clean skip if it's missing
    (a wrong repo-root resolution / a partial checkout) — never a
    false-fail because the installer can't even be found."""
    tg = _tg_launcher()
    if not tg.exists():
        pytest.skip(
            f"installer launcher not found at {tg} — cannot drive "
            "`tg install` (wrong repo root / partial checkout).")
    return tg


def _seeded_env() -> dict:
    """The subprocess env for a --non-interactive install: inherit the
    current env (AWS_PROFILE, TG_* seeded answers the operator/CI set)
    and pin the profile from E2E_AWS_PROFILE when given.

    The installer reads its answers from env in --non-interactive mode
    (_seed_answers in __main__.py: AWS_REGION, TG_TARGET_ACCOUNT_ID,
    TG_ALLOWED_INGRESS_CIDRS, TG_BOOTSTRAP_ADMIN_EMAIL, TG_CERT_ARN,
    TG_VPC_ID, TG_SUBNET_IDS, AWS_PROFILE, …). We do NOT invent those
    here — we pass through whatever the operator seeded and only bridge
    the E2E_* config names the live tier already uses."""
    env = dict(os.environ)
    profile = os.environ.get("E2E_AWS_PROFILE")
    if profile:
        env["AWS_PROFILE"] = profile
    return env


# ── the canonical DEFAULT answer-set (#1446) ─────────────────────────
#
# The single documented source for "what a customer who accepts the
# sensible defaults supplies." --non-interactive has NO press-Enter
# defaults mode (_seed_answers hard-fails on a missing REQUIRED answer),
# so the default install still needs an explicit env — but ONLY the
# answers that genuinely have no sensible default (the operator's target
# account, bootstrap email, and AWS profile). Everything else takes the
# installer's/wizard's own default:
#   region       → us-east-1 (wizard _q_region default; #env AWS_REGION)
#   ingress_cidrs→ 0.0.0.0/0 (valid with the login wall on — the default
#                  posture; the login wall is the barrier)
#   image        → 'build' (wizard _q_image default) — NOT set here; the
#                  installer defaults it, so a default install omits it
#   VPC          → create-new (TG_VPC_ID/TG_SUBNET_IDS unset → the
#                  installer creates its own 2-AZ VPC, the default path)
#   TLS/cert     → the installer's default (no TG_CERT_ARN → its default
#                  cert mode)
# auth_provider is pinned to Cognito by _seed_answers regardless.
_REQUIRED_OPERATOR_ANSWERS = (
    "TG_TARGET_ACCOUNT_ID",   # the target account — no default
    "TG_BOOTSTRAP_ADMIN_EMAIL",  # the first admin — no default
    # AWS_PROFILE / E2E_AWS_PROFILE — the creds — no default
)


def _default_answers() -> dict:
    """The canonical default answer-set: the installer's own defaults for
    everything that has one, so only the genuinely-required operator
    answers remain env-supplied. Returns the TG_*/AWS_* the DEFAULT
    install adds on top of the required operator answers."""
    return {
        "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        # Open ingress is the default posture with the login wall on;
        # override with E2E_INSTALL_INGRESS to lock it to an IP.
        "TG_ALLOWED_INGRESS_CIDRS": os.environ.get(
            "E2E_INSTALL_INGRESS", "0.0.0.0/0"),
        # image / VPC / cert intentionally OMITTED — a default install
        # takes the installer's own defaults (build / create-new / …).
    }


def _default_install_env() -> dict:
    """Build the subprocess env for a DEFAULT install: the required
    operator answers (from the ambient env / E2E_AWS_PROFILE) PLUS the
    default answer-set — and NOTHING else TG_*. Starting from a scrubbed
    env (no stray TG_* an operator may have exported) is what lets the
    test assert 'a default install needs no TG_* beyond the documented
    set + the required operator answers.'

    Skips with a clear reason if a required-no-default answer is missing
    (the install genuinely can't proceed without it)."""
    base = {}
    # Carry through non-TG_ env the subprocess needs (PATH, HOME, AWS_*,
    # etc.) but DROP every TG_* so only our default-set TG_* remain.
    for k, v in os.environ.items():
        if not k.startswith("TG_"):
            base[k] = v
    # Required operator answers — must be present (no sensible default).
    profile = os.environ.get("E2E_AWS_PROFILE") or os.environ.get(
        "AWS_PROFILE")
    if not profile:
        pytest.skip(
            "default install needs AWS creds: set E2E_AWS_PROFILE "
            "(or AWS_PROFILE) — the profile has no default.")
    base["AWS_PROFILE"] = profile
    missing = [k for k in _REQUIRED_OPERATOR_ANSWERS
               if not os.environ.get(k)]
    if missing:
        pytest.skip(
            "default install needs the required operator answers "
            f"{missing} — these have no sensible default (target "
            "account, bootstrap email). Set them to run the "
            "default-path install.")
    for k in _REQUIRED_OPERATOR_ANSWERS:
        base[k] = os.environ[k]
    # Layer the documented defaults on top.
    base.update(_default_answers())
    return base


def _run_installer(args, timeout: int,
                   env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `scripts/tg <args>` --non-interactive, capturing stdout+stderr.

    `env` overrides the subprocess env (default: the operator-passthrough
    `_seeded_env()`; the default-path test passes `_default_install_env()`
    so only the documented default answer-set + required answers apply).

    Returns the CompletedProcess. A non-zero exit is the caller's to
    assert on (per testing.md: a failed install is a FAIL, not a SKIP);
    an inability to even spawn the process is translated to a skip."""
    tg = _require_launcher()
    cmd = [str(tg), *args]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            env=env if env is not None else _seeded_env(), timeout=timeout)
    except FileNotFoundError as e:  # no python3 / launcher unrunnable
        pytest.skip(f"cannot execute the installer ({cmd[0]}): {e}")
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"installer timed out after {timeout}s: {' '.join(cmd)} — "
            "a real deploy exceeded the budget; raise E2E_INSTALL_TIMEOUT "
            "or investigate a hung deploy.")


def _combined(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def _describe_stack_status(aws_session, region: str) -> str | None:
    """The tg-container-stack StackStatus via boto3, or None if the
    stack doesn't exist. Any describe error other than a genuine
    'does not exist' propagates as None (caller treats as 'no stack')."""
    cfn = aws_session.client("cloudformation", region_name=region)
    try:
        resp = cfn.describe_stacks(StackName=CONTAINER_STACK)
    except Exception:  # noqa: BLE001 — no-stack / no-perms → treat as absent
        return None
    stacks = resp.get("Stacks") or []
    if not stacks:
        return None
    return stacks[0].get("StackStatus")


def _assert_identity_report(out: str, account_id: str) -> None:
    """The preflight identity report (__main__.py cmd_install): the
    resolved account MUST equal the target `account_id` fixture — the
    installer is deploying into the demo2 target we asserted, not some
    ambient account. The two lines are printed unconditionally at the
    top on a non-dry-run install."""
    assert "Using AWS credentials:" in out, (
        "installer did not print the 'Using AWS credentials:' preflight "
        "line — the identity report is expected at the top of a "
        "non-dry-run install.")
    assert "Logged in as:" in out, (
        "installer did not print the 'Logged in as:' identity line.")
    # The resolved account appears in "Logged in as: <arn> (account
    # <acct>)." — assert it's the target account we verified via boto3.
    assert f"(account {account_id})" in out, (
        f"installer's resolved account is not the target {account_id} — "
        "the 'Logged in as: … (account <acct>)' line did not carry the "
        f"demo2 target account.\n---installer output tail---\n{out[-2000:]}")


def _assert_confirm_header(out: str) -> None:
    """The confirm screen header (render_confirm). Prefer the exact
    em-dash form `tg install — confirm`; fall back to an 'install' +
    'confirm' same-line match if the em-dash didn't round-trip through
    the captured stream."""
    if "tg install — confirm" in out:
        return
    header_line = any(
        ("install" in ln and "confirm" in ln)
        for ln in out.splitlines())
    assert header_line, (
        "installer did not print the confirm-screen header "
        "('tg install — confirm') — the go/no-go screen is expected "
        f"before any mutation.\n---output tail---\n{out[-2000:]}")


def _assert_version_at_installed_sha(live_client) -> None:
    """After install, GET /api/version is 200 with a non-empty version.
    When E2E_INSTALL_SHA is set (the same-SHA discipline), assert the
    deployed version carries it; otherwise assert 200 + non-empty and
    note the SHA wasn't pinned."""
    r = live_client.get("/api/version")
    assert r.status == 200, (
        f"/api/version returned {r.status} after install, not 200 — the "
        f"app is not live at the installed build. Body: {r.text[:300]}")
    version = (r.json() or {}).get("version") or ""
    assert version, "/api/version returned an empty version string."
    sha = os.environ.get("E2E_INSTALL_SHA", "").strip()
    if sha:
        assert sha in version, (
            f"deployed /api/version is {version!r} but the installed SHA "
            f"was pinned to {sha!r} — the running app does not match the "
            "candidate build (same-SHA discipline).")
    # else: SHA not pinned — 200 + non-empty version is all we can assert.


# ── 1. UPGRADE (default, non-destructive) ────────────────────────────

def test_install_upgrade_on_live_stack(
        live_base, aws_session, account_id, live_client):
    """Re-run `tg install --non-interactive` over a LIVE tg-container-
    stack and assert the installer takes the in-place UPGRADE path
    (#962), reaches UPDATE_COMPLETE, and pre-seeded data survives.

    Honest-skip when there's no live tg-container-stack to upgrade, or
    it's in a non-terminal (non-updatable) state — a re-install over a
    mid-operation stack is a wrong action, not a test failure.

    live_client is the authenticated admin client the live tier
    resolves (test-trust / creds / saml-human via login_strategy); it
    seeds the data-survival marker + reads /api/version and /api/users."""
    region = os.environ.get("AWS_REGION", "us-east-1")

    status = _describe_stack_status(aws_session, region)
    if status is None:
        pytest.skip(
            f"no live {CONTAINER_STACK} to upgrade in {region} (account "
            f"{account_id}) — the UPGRADE path needs an existing stack. "
            "Run the NEW-install path (E2E_INSTALL_DESTROY=1) first, or "
            "point at a target that already has tg installed.")
    if not status.endswith("_COMPLETE") or "ROLLBACK" in status:
        pytest.skip(
            f"{CONTAINER_STACK} is in {status} — not a terminal, "
            "updatable state; wait for the in-progress operation to "
            "finish before an UPGRADE re-run.")

    # Seed a marker row BEFORE the upgrade (data-survival check). Reuse
    # the reserved @example.com principal via the real onboard endpoint;
    # a 409 (already there) is success — idempotent.
    client = live_client
    seed = client.post(
        "/api/users/preregister", json_body={"email": SEED_EMAIL})
    assert seed.status in (200, 201, 409), (
        f"could not seed the pre-upgrade marker {SEED_EMAIL}: "
        f"{seed.status} {seed.text[:300]}")

    try:
        proc = _run_installer(
            ["install", "--non-interactive"], INSTALL_TIMEOUT)
        out = _combined(proc)

        # A non-zero installer exit is a FAIL (testing.md: a failed
        # install is not a SKIP). Surface the captured output.
        assert proc.returncode == 0, (
            f"`tg install` (upgrade) exited {proc.returncode}, expected "
            f"0.\n---installer output tail---\n{out[-3000:]}")

        # Preflight identity report → the target account.
        _assert_identity_report(out, account_id)
        # Confirm screen header.
        _assert_confirm_header(out)
        # UPGRADE mode line (#962), whitespace-tolerant.
        assert _MODE_UPGRADE_RE.search(out), (
            "confirm screen did not show the UPGRADE mode line "
            "('Mode : UPGRADE existing tg-container-stack') — a re-run "
            "over a live stack must take the in-place upgrade path, not a "
            f"NEW install.\n---output tail---\n{out[-2000:]}")
        # If the image changed, the from→to line appears (best-effort:
        # an Enter-through same-image upgrade legitimately omits it).
        if _IMAGE_ARROW_RE.search(out):
            assert "→" in out  # sanity: the arrow rendered

        # Done banner (#1119 last-print guarantee) + CUR advisory.
        assert "✓ Install complete — tg is running." in out, (
            "installer did not print the '✓ Install complete — tg is "
            "running.' done-banner — the #1119 last-print guarantee "
            f"failed.\n---output tail---\n{out[-2000:]}")
        assert "Cost reporting: configured" in out, (
            "installer did not print the 'Cost reporting: configured' "
            "CUR advisory line after a cloud install.")

        # Real CFN terminal state: UPDATE_COMPLETE, never a rollback.
        post_status = _describe_stack_status(aws_session, region)
        assert post_status == "UPDATE_COMPLETE", (
            f"{CONTAINER_STACK} is {post_status} after the upgrade, "
            "expected UPDATE_COMPLETE.")
        assert post_status and "ROLLBACK" not in post_status, (
            f"{CONTAINER_STACK} rolled back on the upgrade "
            f"({post_status}).")

        # App live at the installed SHA.
        _assert_version_at_installed_sha(client)

        # UPGRADE-only data survival: the pre-seeded principal is still
        # listed (a rolling deploy does NOT reset the DB).
        users = client.get("/api/users")
        assert users.status == 200, (
            f"/api/users returned {users.status} after the upgrade, not "
            f"200. Body: {users.text[:300]}")
        emails = {
            u.get("email") for u in (users.json() or {}).get("users", [])}
        assert SEED_EMAIL in emails, (
            f"the pre-seeded principal {SEED_EMAIL} is missing from "
            "/api/users after the upgrade — data did NOT survive the "
            "rolling deploy (a DB reset regression).")
    finally:
        # Best-effort marker cleanup — never mask the test's own result.
        try:
            client.delete(f"/api/users/{SEED_EMAIL}")
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


# ── 2. NEW clean-room (operator-only, destructive) ───────────────────

@pytest.mark.skipif(
    os.environ.get("E2E_INSTALL_DESTROY") != "1",
    reason="destructive NEW-install path is operator-only: set "
           "E2E_INSTALL_DESTROY=1 to tear down + fresh-install the "
           "target. Skipped by default so no run destroys a stack.")
def test_install_new_clean_room(
        live_base, aws_session, account_id, live_client):
    """Tear the target down, then a FRESH `tg install --non-interactive`,
    and assert the NEW-install checkpoints (Mode: NEW install →
    CREATE_COMPLETE, app live at the installed SHA).

    DESTROY INTERFACE (read from tg_cli/__main__.py cmd_destroy, NOT
    guessed): `tg destroy --non-interactive --full` — --non-interactive
    threads TG_NONINTERACTIVE=1 so the bash teardown suppresses its
    prompts, and --full also removes tg-cur-athena (else it orphans the
    CUR stack + its S3 buckets, #922/#1075). This is the documented,
    idempotent clean-slate path; we use it rather than invoking
    tg-ecs-destroy.sh directly."""
    region = os.environ.get("AWS_REGION", "us-east-1")

    # Teardown via the CLI's own destroy interface (idempotent — a no-op
    # if nothing is deployed). A non-zero destroy exit is a FAIL.
    destroy = _run_installer(
        ["destroy", "--non-interactive", "--full"], DESTROY_TIMEOUT)
    d_out = _combined(destroy)
    assert destroy.returncode == 0, (
        f"`tg destroy --non-interactive --full` exited "
        f"{destroy.returncode}, expected 0 — the clean-room teardown "
        f"failed.\n---destroy output tail---\n{d_out[-3000:]}")

    # After a --full destroy the stack must be gone (a NEW install then
    # takes the create path, not an upgrade of a leftover stack).
    pre_status = _describe_stack_status(aws_session, region)
    assert pre_status is None, (
        f"{CONTAINER_STACK} still exists ({pre_status}) after "
        "`tg destroy --full` — the NEW-install path needs a clean slate.")

    # Fresh install.
    proc = _run_installer(["install", "--non-interactive"], INSTALL_TIMEOUT)
    out = _combined(proc)
    assert proc.returncode == 0, (
        f"`tg install` (new) exited {proc.returncode}, expected 0.\n"
        f"---installer output tail---\n{out[-3000:]}")

    _assert_identity_report(out, account_id)
    _assert_confirm_header(out)
    # NEW mode line (render_confirm else-branch), whitespace-tolerant.
    assert _MODE_NEW_RE.search(out), (
        "confirm screen did not show the NEW mode line "
        "('Mode : NEW install') — a fresh install over a clean slate "
        f"must take the create path.\n---output tail---\n{out[-2000:]}")

    assert "✓ Install complete — tg is running." in out, (
        "installer did not print the '✓ Install complete — tg is "
        "running.' done-banner on the NEW install.")
    assert "Cost reporting: configured" in out, (
        "installer did not print the 'Cost reporting: configured' CUR "
        "advisory line on the NEW install.")

    # Real CFN terminal state: CREATE_COMPLETE, never a rollback.
    post_status = _describe_stack_status(aws_session, region)
    assert post_status == "CREATE_COMPLETE", (
        f"{CONTAINER_STACK} is {post_status} after the NEW install, "
        "expected CREATE_COMPLETE.")
    assert post_status and "ROLLBACK" not in post_status, (
        f"{CONTAINER_STACK} rolled back on the NEW install "
        f"({post_status}).")

    # App live at the installed SHA — reuse the admin client resolved by
    # the live tier (its headers were fixed at fixture-setup, before the
    # destroy; they stay valid once the freshly-installed app is up).
    _assert_version_at_installed_sha(live_client)


# ── 3. DEFAULT answer-set (#1446) ────────────────────────────────────

def test_install_with_defaults(
        live_base, aws_session, account_id, live_client):
    """A `tg install --non-interactive` with ONLY the canonical DEFAULT
    answer-set (+ the required operator answers) stands up a working
    stack — 'a customer who accepts the sensible defaults gets a working
    install.'

    Unlike test_install_upgrade_on_live_stack (which passes through
    whatever TG_* the operator seeded), this builds a scrubbed env
    carrying NO TG_* beyond the documented default set (_default_answers)
    + the required operator answers — so it also proves a default install
    needs nothing more. It exercises whichever CFN path the target is in
    (create when absent, update when present), asserting the same
    observable checkpoints #1443 uses.

    Honest-skip when no E2E_API_BASE (live_base) / no creds (aws_session)
    / a required-no-default answer is missing (_default_install_env)."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    pre_status = _describe_stack_status(aws_session, region)
    if pre_status is not None and (
            not pre_status.endswith("_COMPLETE")
            or "ROLLBACK" in pre_status):
        pytest.skip(
            f"{CONTAINER_STACK} is {pre_status} — not a terminal state; "
            "wait for the in-progress operation before a default install.")

    # Build the default-path env FIRST — it skips (before any mutation)
    # if a required operator answer is missing.
    env = _default_install_env()

    # Assert the default env adds NO TG_* beyond the documented set: the
    # required operator answers + _default_answers()'s TG_* keys. This is
    # the "a default install needs nothing more" guarantee (#1446 AC).
    allowed_tg = set(_REQUIRED_OPERATOR_ANSWERS) | {
        k for k in _default_answers() if k.startswith("TG_")}
    extra_tg = {k for k in env if k.startswith("TG_")} - allowed_tg
    assert not extra_tg, (
        f"the default install env carries unexpected TG_* {extra_tg} — a "
        "default install must need only the documented default set + the "
        "required operator answers (a stray TG_* means the default path "
        "secretly depends on more).")

    # Seed a marker only if we're upgrading a live stack (data-survival
    # is meaningful on an update, not a fresh create).
    upgrading = pre_status is not None
    if upgrading:
        seed = live_client.post(
            "/api/users/preregister", json_body={"email": SEED_EMAIL})
        assert seed.status in (200, 201, 409), (
            f"could not seed pre-install marker {SEED_EMAIL}: "
            f"{seed.status} {seed.text[:300]}")

    try:
        proc = _run_installer(
            ["install", "--non-interactive"], INSTALL_TIMEOUT, env=env)
        out = _combined(proc)
        assert proc.returncode == 0, (
            f"`tg install` (defaults) exited {proc.returncode}, expected "
            f"0 — a default-answer install did not succeed.\n"
            f"---installer output tail---\n{out[-3000:]}")

        # Same observable checkpoints as the other install paths.
        _assert_identity_report(out, account_id)
        _assert_confirm_header(out)
        assert "✓ Install complete — tg is running." in out, (
            "installer did not print the '✓ Install complete' done-banner "
            f"on the default install.\n---output tail---\n{out[-2000:]}")
        assert "Cost reporting: configured" in out, (
            "installer did not print the 'Cost reporting: configured' CUR "
            "advisory on the default install.")

        # CFN reached terminal success (create OR update — whichever the
        # target's starting state implied), never a rollback.
        post_status = _describe_stack_status(aws_session, region)
        assert post_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"), (
            f"{CONTAINER_STACK} is {post_status} after the default "
            "install, expected CREATE_COMPLETE or UPDATE_COMPLETE.")
        assert post_status and "ROLLBACK" not in post_status, (
            f"{CONTAINER_STACK} rolled back on the default install "
            f"({post_status}).")

        # App live at the installed SHA.
        _assert_version_at_installed_sha(live_client)
    finally:
        if upgrading:
            try:
                live_client.delete(f"/api/users/{SEED_EMAIL}")
            except Exception:  # noqa: BLE001 — teardown best-effort
                pass
