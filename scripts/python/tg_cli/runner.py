"""Execute the bash installers/destroyer — the CLI owns no deploy logic.

`tg install`  → scripts/tg-ecs-install.sh
`tg destroy`  → scripts/tg-ecs-destroy.sh  (the clean-slate verifier)
self-signed   → scripts/tg-make-selfsigned-cert.sh (captures the ARN)
`tg status`   → aws cloudformation describe-stacks (read-only)

--dry-run stops before any of these mutate: it prints the confirm
screen + the exact TG_* env that WOULD be exported, and returns.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# repo root = three levels up from this file
# (scripts/python/tg_cli/runner.py → repo root)
REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"

INSTALL_SH = SCRIPTS / "tg-ecs-install.sh"
DESTROY_SH = SCRIPTS / "tg-ecs-destroy.sh"
SELFSIGNED_SH = SCRIPTS / "tg-make-selfsigned-cert.sh"
# #530 phase 2: the CLI delegates to these engine scripts too
# (coexist with the documented script path — open-Q4 answered).
LOCAL_INSTALL_SH = SCRIPTS / "tg-local-install.sh"
LOCAL_DESTROY_SH = SCRIPTS / "tg-local-destroy.sh"
CUR_DEPLOY_SH = SCRIPTS / "tg-cur-deploy.sh"
CUR_DESTROY_SH = SCRIPTS / "tg-cur-destroy.sh"
VERIFY_CUR_SH = SCRIPTS / "verify-cur.sh"
# #1123: the CUR CFN template carrying Metadata.TgMinImageVersion — the
# install-time image<->template compat gate reads its marker.
CUR_DEPLOY_TEMPLATE = REPO_ROOT / "cfn" / "tg-cur-athena.yaml"
# #576: admin-ui-publish.sh removed with the tg-admin desktop client.

# #1115: the minimum Python the installer supports — the SINGLE source
# of truth the gate reads (no hardcoded duplicate). There is no
# pyproject.toml/setup.cfg in this repo (the CLI ships as loose modules
# under scripts/python), so the constant lives here.
#
# Floor determined EMPIRICALLY (not from the `str | None` PEP-604 unions,
# which only LOOK like 3.10): every tg_cli module carries
# `from __future__ import annotations`, so those unions are deferred
# strings in signatures + dataclass fields and never evaluate at runtime
# (nothing calls typing.get_type_hints / introspects the annotations).
# Verified via `docker run python:3.7|3.8|3.9` — import + `tg --help` +
# `tg --version` all run clean down to 3.7. We pin 3.9 (not lower)
# deliberately: 3.7/3.8 are EOL/security-unsupported, 3.9 is the lowest
# still-maintained version and is verified-clean — gating here rejects
# no working, supported host while still failing fast on a genuinely
# stale interpreter. (NOT 3.10 — that would reject working 3.9; NOT
# 3.12, the CI target ≠ the customer floor.)
TG_MIN_PYTHON = (3, 9)


# #881: display labels for the "collected so far" resume summary.
# Ordered (dict preserves insertion order) so the summary reads in
# the same sequence as the wizard asks. Labels mirror the confirm
# screen's wording (render_confirm below) so the two surfaces agree.
# Only NON-SECRET, persisted answer keys appear here — config.py
# never writes secrets, and resume_summary filters defensively too.
RESUME_SUMMARY_LABELS = {
    "region": "Region",
    "account_id": "Account",
    "vpc_id": "VPC",
    "subnet_ids": "Subnets",
    "ingress_cidrs": "Ingress CIDRs",
    "image": "Image",
    "iam_prefix": "IAM prefix",
    "cert_mode": "TLS",
    "cert_arn": "Certificate ARN",
    "bootstrap_email": "Bootstrap admin",
    "auth_provider": "Login provider",
    "oidc_issuer": "OIDC issuer",
    "oidc_client_id": "OIDC client ID",
}


def resume_summary(answers: dict, secret_keys=frozenset()) -> str:
    """#881: a terse "collected so far" replay for a resumed install.

    Lists only the already-answered, non-secret keys (in
    RESUME_SUMMARY_LABELS order) so an operator re-running after a
    Ctrl-C sees what was retained before the next prompt. Secrets are
    filtered defensively (they're never persisted, but never echo them
    even if a caller stuffs one into `answers`). Returns '' when there's
    nothing to show, so the caller can skip printing entirely."""
    rows = []
    for key, label in RESUME_SUMMARY_LABELS.items():
        if key in secret_keys:
            continue
        val = answers.get(key)
        if val in (None, ""):
            continue
        rows.append(f"    {label:<17}: {val}")
    if not rows:
        return ""
    return (
        "  Resuming — collected so far:\n"
        + "\n".join(rows)
        + "\n  Continuing with the remaining questions…"
    )


def render_confirm(answers: dict, env: dict, local: bool = False) -> str:
    """The confirm screen — what will be created, before any mutation.

    `local=True` (the --local docker-compose path, #530 phase 2)
    drops the ECS/ALB-only rows that path doesn't use."""
    login_on = str(answers.get("enable_login", "y")).lower() in (
        "y", "yes", "true", "1"
    )
    header = ("tg install --local — confirm" if local
              else "tg install — confirm")
    lines = [
        "",
        f"──────────── {header} ────────────",
    ]
    # #962: frame a detected re-run as an UPGRADE of the live stack
    # (vs a NEW install), with the image from→to so the operator sees
    # exactly what this run changes. _is_upgrade is set by cmd_install
    # when a deployed tg-container-stack was found.
    if answers.get("_is_upgrade"):
        img_from = answers.get("_image_from") or "(current)"
        img_to = answers.get("image") or "(unchanged)"
        lines.append("  Mode              : UPGRADE existing "
                     "tg-container-stack")
        if img_from != img_to:
            lines.append(f"  Image             : {img_from}  →  {img_to}")
    else:
        lines.append("  Mode              : NEW install")
    lines += [
        f"  Region            : {answers.get('region')}",
        f"  Account           : {answers.get('account_id', '(from caller)')}",
    ]
    if not local:
        lines += [
            f"  Ingress CIDRs     : {answers.get('ingress_cidrs')}",
            f"  Image             : {answers.get('image')}",
            f"  IAM prefix        : {answers.get('iam_prefix', 'tg-')}",
        ]
    lines += [
        f"  Log group         : {answers.get('log_group')}",
        f"  Bootstrap admin   : {answers.get('bootstrap_email')}",
    ]
    if local:
        lines.append("  Deploys           : docker-compose (postgres + api "
                     "+ worker) on this host")
    else:
        # #778: the "created" line must match the actual plan. On the
        # BYO-VPC path (#774) tg creates NO VPC — it reuses the supplied
        # one — so promising "VPC (2-AZ)" on the go/no-go screen is
        # exactly wrong. A non-empty vpc_id is the canonical BYO signal
        # (same rule the wizard + to_env use: empty = create-new).
        byo_vpc = bool(answers.get("vpc_id"))
        lines.append(f"  TLS               : {answers.get('cert_mode')}")
        if byo_vpc:
            subnets = answers.get("subnet_ids") or "(supplied)"
            lines.append(
                "  Created           : RDS Postgres, ALB, ECS Fargate "
                f"(into existing VPC {answers.get('vpc_id')}, "
                f"subnets {subnets})"
            )
        else:
            lines.append(
                "  Always created    : VPC (2-AZ), RDS Postgres, ALB, "
                "ECS Fargate"
            )
    if login_on:
        # #796: the cognito path needs no issuer + isn't two-phase (tg
        # stands up the pool inline); only the bring-your-own-Okta path
        # asks for an issuer and pauses for the redirect-URI registration.
        is_okta = bool(answers.get("oidc_issuer"))
        if is_okta:
            lines += [
                "  Login wall        : on — Okta/OIDC (issuer "
                f"{answers.get('oidc_issuer')})",
                "                      two-phase: deploy pauses for the",
                "                      redirect URI, then resumes",
            ]
        else:
            lines += [
                "  Login wall        : on — Cognito (tg stands up the "
                "login; no Okta needed)",
            ]
    else:
        lines.append("  Login wall        : OFF (only safe behind a tight CIDR allowlist)")
    lines.append("──────────────────────────────────────────────")
    return "\n".join(lines)


def _merged_env(env: dict) -> dict:
    merged = os.environ.copy()
    merged.update({k: str(v) for k, v in env.items() if v not in (None, "")})
    return merged


def make_selfsigned(cn: str, env: dict) -> str:
    """Run the cert helper, return the printed ARN (stdout-only contract)."""
    cmd = [str(SELFSIGNED_SH), "--cn", cn]
    proc = subprocess.run(
        cmd, env=_merged_env(env), capture_output=True, text=True
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError("tg-make-selfsigned-cert.sh failed")
    return proc.stdout.strip().splitlines()[-1].strip()


def run_install(env: dict) -> tuple[int, dict]:
    """Exec the installer with the TG_* env, streaming its output live
    (the deploy is long — the operator watches it). Returns
    (exit_code, summary) where summary is the KEY=VALUE block the script
    writes to TG_SUMMARY_OUT (#1119) — the values the WIZARD needs to
    print the single 'Done' banner LAST, after CUR. summary is {} if the
    file wasn't written (older script / a failure before the summary).

    #1119: the installer no longer prints its own '== Done ==' summary
    under the wizard (it would land BEFORE 'deploying CUR…'); it emits
    the values here instead and the orchestrator owns the banner."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="tg-summary-", suffix=".env")
    os.close(fd)
    env = dict(env)
    env["TG_SUMMARY_OUT"] = path
    try:
        rc = subprocess.call([str(INSTALL_SH)], env=_merged_env(env))
        return rc, _read_summary_file(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_summary_file(path: str) -> dict:
    """Parse the installer's KEY=VALUE summary file (#1119). Returns {}
    if absent/unreadable — the caller falls back gracefully (no banner
    crash on a missing handoff)."""
    out: dict = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _live_logs_cmd(profile_hint: str, region: str) -> str:
    """The copy-paste-valid `aws logs tail` command for the advanced
    block. profile_hint is the installer's PROFILE_HINT ('--profile <p>'
    when a profile is set, '' otherwise) — but the KEY=VALUE summary
    round-trip can drop its trailing space, which produced the
    `...install--region` bug (no space before --region). So we normalize
    here: strip the hint and re-join with exactly one space, omitting
    --profile entirely (no dangling flag / double space) when unset."""
    hint = (profile_hint or "").strip()
    parts = ["aws logs tail /ecs/tg-container --follow"]
    if hint:
        parts.append(hint)
    parts.append(f"--region {region}")
    return " ".join(parts)


def render_done_banner(summary: dict, cur_line: str | None = None,
                       verbose: bool = False) -> str:
    """#1119: the single '== Done ==' banner the WIZARD prints LAST,
    after BOTH ECS and CUR succeed. The orchestrator owns it so it
    structurally cannot print before a later step (the Done-before-CUR
    defect) and so the (now-hoisted) CUR decision never blocks
    mid-install.

    `summary` is the KEY=VALUE dict from run_install; `cur_line` is the
    cost-reporting status sentence (None on --local). `verbose` gates the
    'Advanced / troubleshooting' block — concise by default (open-URL +
    sign-in + cost-reporting), the diagnostic links/commands only under
    `tg install --verbose` (the conventional concise-default / -v-for-
    detail CLI shape)."""
    scheme = summary.get("API_SCHEME", "https")
    host = summary.get("API_HOST", "")
    signin = summary.get("SIGNIN_AS", "the admin email you configured")
    provider = summary.get("LOGIN_PROVIDER", "Cognito")
    region = summary.get("AWS_REGION", "us-east-1")
    profile_hint = summary.get("PROFILE_HINT", "")

    lines = [
        "",
        "✓ Install complete — tg is running.",
        "",
        "Next step — open the admin console and sign in:",
        f"  1. Open:        {scheme}://{host}/",
        f"  2. Sign in as:  {signin}",
        "     (you are the first admin; you'll set up everyone "
        "else from here)",
        f"  3. First sign-in uses the {provider} login you configured.",
    ]
    if provider == "Cognito":
        lines += [
            "",
            "Signing in (you are the first admin):",
            "  • If you set an admin password during install, sign in "
            "with it.",
            '  • Otherwise click "Forgot password" on the login page — '
            "a reset",
            f"    code is sent to {signin}; set your password, then "
            "sign in.",
            "  (Forgot password works because the admin is "
            "pre-confirmed.)",
        ]
    lines += [
        "",
        "Set up alerts (optional but recommended):",
        "  Open the admin console → Settings → Notifications and add",
        "  an SMTP relay (for email) and/or a Slack/webhook URL.",
        "  Spend-cap alerts won't deliver until a transport is set.",
    ]
    if cur_line:
        lines += ["", cur_line]
    # The advanced block is noise for the common case — gate it behind
    # --verbose. Default banner ends after sign-in + cost-reporting.
    if verbose:
        lines += [
            "",
            "Advanced / troubleshooting:",
            f"  Health check   {scheme}://{host}/api/version",
            f"  API docs       {scheme}://{host}/docs",
            "  ECS console    https://us-east-1.console.aws.amazon.com/"
            "ecs/v2/clusters/tg-cluster",
            f"  Live logs      {_live_logs_cmd(profile_hint, region)}",
            "  Tear down      scripts/tg-ecs-destroy.sh",
        ]
    return "\n".join(lines)


def cur_reuse_candidate(region: str, profile: str | None) -> str | None:
    """#1119: read-only probe — is there an existing CUR 2.0 export this
    install could reuse? Returns the export NAME to offer for reuse, or
    None (no candidate → the wizard doesn't ask; CUR creates its own).

    Delegates the real validation to tg-cur-deploy.sh's detect/classify
    logic at deploy time; this is only a lightweight "should the wizard
    ASK?" signal so we never pop a reuse question when there's nothing to
    reuse. Lists exports via `bcm-data-exports` and returns the first
    whose name looks like a tg/bedrock CUR export. Any error → None (ask
    nothing; the deploy still does the authoritative classify + safe
    default)."""
    import json
    cmd = ["aws", "bcm-data-exports", "list-exports",
           "--region", region, "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None
    for exp in data.get("Exports", []):
        name = (exp.get("ExportName") or exp.get("Name") or "").strip()
        if name and ("cur" in name.lower() or "bedrock" in name.lower()):
            return name
    return None


def run_destroy(env: dict, full: bool = False) -> int:
    cmd = [str(DESTROY_SH)]
    if full:
        cmd.append("--full")
    return subprocess.call(cmd, env=_merged_env(env))


# ── #530 phase 2: delegating wrappers (coexist with the scripts;
#    the CLI owns no logic, it just execs them by path) ──────────

def run_local_install(env: dict) -> int:
    """`tg install --local` → the docker-compose dev installer."""
    return subprocess.call([str(LOCAL_INSTALL_SH)], env=_merged_env(env))


def run_local_destroy(env: dict) -> int:
    """`tg destroy --local` → the docker-compose teardown."""
    return subprocess.call([str(LOCAL_DESTROY_SH)], env=_merged_env(env))


def run_cur_deploy(env: dict) -> int:
    """CUR 2.0 + Athena stack (tg-cur-athena). #922: deployed by
    default as a core install step, not an opt-in add-on."""
    return subprocess.call([str(CUR_DEPLOY_SH)], env=_merged_env(env))


def run_cur_destroy(env: dict) -> int:
    """`tg destroy --full` → tear down tg-cur-athena too (#922). CUR
    is default-on now, so a --full teardown must remove it rather than
    orphan the stack + its S3 buckets. Idempotent — a no-op when the
    stack is already gone."""
    return subprocess.call([str(CUR_DESTROY_SH)], env=_merged_env(env))


def run_verify_cur(env: dict) -> int:
    """`tg install --verify` → the CUR-wiring verifier."""
    return subprocess.call([str(VERIFY_CUR_SH)], env=_merged_env(env))


def run_captured(script: Path, env: dict) -> tuple[int, str]:
    """#996: run an engine script but CAPTURE its stdout+stderr instead
    of streaming it to the terminal, returning (rc, combined_output).
    Used on the `tg install` happy path so the CUR-deploy / CUR-verify
    chatter (Athena SQL, S3-inspect, teardown, the raw result dump)
    never becomes the install's closing screen — the orchestrator
    replays the captured text ONLY on failure, and otherwise folds a
    single plain-language 'spend data in ~24h' line into the final
    summary. The scripts themselves are unchanged — run standalone they
    still print everything (so this also avoids editing verify-cur.sh /
    tg-cur-deploy.sh, dodging a collision with the in-flight verify-cur
    work)."""
    proc = subprocess.run(
        [str(script)], env=_merged_env(env),
        capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def build_version() -> str:
    """#1000/#1088: the source/build version — `v<release>-g<sha>` — that
    feeds tg-ecs-install.sh's TgVersion task-def stamp (what /api/version
    + the UI footer report, #538/#791). The CLI derives it ONCE here and
    the installer honors a pre-set TG_VERSION, so the start banner and
    the deploy stamp can't drift. NOT the static __version__ (0.1.0) that
    powers `tg --version`.

    #1088: the release comes from the committed root VERSION file, NOT a
    `git describe` tag. The published `tokengov` repo is a depth-1
    parentless orphan force-replaced on every republish; `git fetch`
    won't move an existing local tag (Git ≥2.20), and `git describe`
    only walks ancestors — so on a stale-tag customer clone describe
    falls through to a bare SHA (the reported bug). The committed VERSION
    file is checkout-independent: it needs no VCS metadata, so it's
    correct on a fresh clone, a stale-tag pull, a shallow clone, or a
    tarball. The annotated tag still exists for tooling/`git checkout
    v1.1.0` (#1014) — the banner just no longer DEPENDS on it.

    Resolution: `v<VERSION>-g<short-HEAD>` (+ `-dirty` when the tree is
    dirty). Fallbacks: bare short SHA when VERSION is unreadable, then
    `"dev"` when even git is unavailable."""
    version = None
    try:
        version = (REPO_ROOT / "VERSION").read_text().strip()
    except OSError:
        version = None

    sha = ""
    dirty = ""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            sha = proc.stdout.strip()
        # `git status --porcelain` non-empty → working tree is dirty.
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5)
        if st.returncode == 0 and st.stdout.strip():
            dirty = "-dirty"
    except (OSError, subprocess.SubprocessError):
        sha = ""

    if version and sha:
        return f"v{version}-g{sha}{dirty}"
    if version:
        return f"v{version}{dirty}"
    if sha:
        return f"{sha}{dirty}"
    return "dev"


def display_version(full: str) -> str:
    """#1104: reduce a full build version to the bare release token for
    HUMAN-FACING displays (the install banner + UI footer). The full
    `v<ver>-g<sha>[-dirty]` is build provenance (#1088) — it stays the
    value stamped into the deploy and returned by /api/version for
    support — but `v1.1.0-ga2c3a69-dirty` reads as clutter where a user
    expects `v1.1.0`.

    A real release string collapses to `v1.1.0`; the fallbacks
    build_version() can return — a bare short SHA (no `v` prefix) or
    `"dev"` — DON'T match the release pattern, so they pass through
    unchanged (a SHA-only build has no release to show, so showing the
    SHA is correct)."""
    import re
    m = re.match(r"^(v\d+\.\d+\.\d+)(?:-g[0-9a-f]+)?(?:-dirty)?$", full)
    return m.group(1) if m else full


def deployed_version(base_url: str, timeout: float = 5.0) -> str | None:
    """#1000: read the DEPLOYED build version from a running app's
    GET <base_url>/api/version (the TgVersion stamp the ECS task
    reports). Returns the version string, or None on any error (app not
    reachable yet / unexpected body) — the caller then just skips the
    match line rather than failing the install."""
    import json
    import urllib.request
    url = base_url.rstrip("/") + "/api/version"
    try:
        ctx = None
        if url.startswith("https"):
            import ssl
            ctx = ssl.create_default_context()
            # A self-signed ALB cert is common on stage/dev; the version
            # read is non-security-critical, so don't fail on chain.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    v = data.get("version") if isinstance(data, dict) else None
    return v or None


def detect_public_ip(timeout: float = 3.0) -> str | None:
    """#875: best-effort current public IP via https://checkip.amazonaws.com
    (the URL the CIDR help text already cites) so the wizard can pre-fill the
    ingress prompt with a `/32`. Returns the dotted-quad string, or None on
    ANY failure (no egress / timeout / unexpected body) — there is NO hard
    dependency on egress; the wizard falls back to manual entry."""
    import re
    import urllib.request

    try:
        with urllib.request.urlopen(
            "https://checkip.amazonaws.com", timeout=timeout
        ) as resp:
            ip = resp.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 — best-effort; any failure → fallback
        return None
    # checkip returns a bare dotted-quad (+ newline); validate the shape.
    return ip if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) else None


# #877 / #876: the prebuilt public container image the publish
# pipeline maintains. Kept in lock-step with the publish script's
# alias/repo + the version contract (git describe --tags --always)
# and the moving channel tag.
PUBLIC_ECR_ALIAS = "e9y1g4o2"
PUBLIC_ECR_REPO = "tg-container"
PUBLIC_ECR_CHANNEL_TAG = "latest"


def image_version() -> str | None:
    """#877: the version that pins source ↔ image — the SAME string the
    installer stamps (tg-ecs-install.sh: `git describe --tags --always`)
    resolved against THIS checkout. So a customer on repo <v> is offered
    image :<v>, a pinned reproducible install. None if git can't resolve
    a version (the caller then falls back to the channel tag)."""
    cmd = ["git", "describe", "--tags", "--always"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    v = proc.stdout.strip()
    return v or None


def public_image_ref(tag: str) -> str:
    """Fully-qualified public image URI for a tag (no auth to pull)."""
    return f"public.ecr.aws/{PUBLIC_ECR_ALIAS}/{PUBLIC_ECR_REPO}:{tag}"


def public_image_available(tag: str, timeout: float = 4.0) -> bool:
    """#877: True iff `public.ecr.aws/<alias>/tg-container:<tag>` is
    actually pullable RIGHT NOW. Public ECR serves the OCI distribution
    API anonymously: fetch a short-lived token, then HEAD the manifest.
    Returns False on ANY failure (offline, 404, pipeline not yet run) so
    the wizard NEVER suggests an unpullable ref — it falls back to build.
    """
    import json
    import urllib.request

    base = "https://public.ecr.aws"
    repo = f"{PUBLIC_ECR_ALIAS}/{PUBLIC_ECR_REPO}"
    try:
        # 1. anonymous token for this repo (public read, no creds).
        tok_url = (
            f"{base}/token/?scope=repository:{repo}:pull"
        )
        with urllib.request.urlopen(tok_url, timeout=timeout) as r:
            token = json.load(r).get("token")
        if not token:
            return False
        # 2. HEAD the manifest by tag; 200 ⇒ the tag exists + is pullable.
        man_url = f"{base}/v2/{repo}/manifests/{tag}"
        req = urllib.request.Request(man_url, method="HEAD")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header(
            "Accept",
            "application/vnd.oci.image.index.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.docker.distribution.manifest.v2+json",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — any failure ⇒ not pullable ⇒ build
        return False


def public_image_digest(tag: str, timeout: float = 4.0) -> str | None:
    """#1059: the manifest digest (`sha256:…`) the public-ECR tag
    currently resolves to, or None on any failure. Same anonymous
    token + HEAD as public_image_available, but returns the
    Docker-Content-Digest header so the upgrade path can compare a
    deployed public tag against `latest` by IDENTITY (digest equality)
    rather than parsing tag formats. None ⇒ 'can't tell' (stay silent,
    never guess — per ground-by-data)."""
    import json
    import urllib.request

    base = "https://public.ecr.aws"
    repo = f"{PUBLIC_ECR_ALIAS}/{PUBLIC_ECR_REPO}"
    try:
        tok_url = f"{base}/token/?scope=repository:{repo}:pull"
        with urllib.request.urlopen(tok_url, timeout=timeout) as r:
            token = json.load(r).get("token")
        if not token:
            return None
        man_url = f"{base}/v2/{repo}/manifests/{tag}"
        req = urllib.request.Request(man_url, method="HEAD")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header(
            "Accept",
            "application/vnd.oci.image.index.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.docker.distribution.manifest.v2+json",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.headers.get("Docker-Content-Digest")
    except Exception:  # noqa: BLE001 — any failure ⇒ can't tell
        return None


def resolve_prebuilt_image(timeout: float = 4.0) -> str | None:
    """#877: the pullable prebuilt public image URI to offer as the
    wizard default, or None if none is resolvable (→ fall back to build).

    Prefer the version pinned to THIS checkout (reproducible); if that
    exact tag isn't published yet, fall back to the moving channel tag
    (`latest`) the pipeline keeps pointed at the newest publish. Never
    returns a ref that isn't actually pullable (each candidate is probed).
    """
    for tag in (image_version(), PUBLIC_ECR_CHANNEL_TAG):
        if tag and public_image_available(tag, timeout=timeout):
            return public_image_ref(tag)
    return None


def resolve_upgrade_image(timeout: float = 4.0) -> str | None:
    """#1059: the wizard default on an UPGRADE re-run — the NEWEST
    published public image (the channel tag `latest`), probed pullable,
    or None if unresolvable (→ build). Distinct from
    resolve_prebuilt_image() (greenfield), which prefers the
    checkout-version pin: an upgrade should move the operator to the
    newest image, not merely to their local checkout's version."""
    if public_image_available(PUBLIC_ECR_CHANNEL_TAG, timeout=timeout):
        return public_image_ref(PUBLIC_ECR_CHANNEL_TAG)
    return None


def newer_public_image_hint(timeout: float = 4.0) -> str | None:
    """Advisory only: a one-line hint if the moving channel tag
    (`latest`) resolves to a DIFFERENT digest than the version-pinned
    tag for this checkout — i.e. the publisher has pushed a newer public
    image than the one this release pins.

    STRICTLY fail-silent: returns None (no hint) on ANY uncertainty —
    offline, either digest unresolvable, no pinned version, or the two
    are equal. The wizard prints the hint when non-None and NEVER blocks
    on it. Reuses the digest reads the wizard already makes (no git, no
    new network shape). The customer can't act on a stale public image
    (only the publisher republishes) — this is informational, the
    lowest-priority layer."""
    pinned = image_version()
    if not pinned or pinned == PUBLIC_ECR_CHANNEL_TAG:
        return None
    latest_dig = public_image_digest(PUBLIC_ECR_CHANNEL_TAG, timeout=timeout)
    pinned_dig = public_image_digest(pinned, timeout=timeout)
    if not latest_dig or not pinned_dig:
        return None  # can't tell → stay silent
    if latest_dig == pinned_dig:
        return None  # in sync → say nothing
    return ("Note: a newer public image may be available "
            "(latest differs from the pinned version for this release).")


_PRIVATE_ECR_RE = re.compile(
    r"\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/", re.IGNORECASE)


def is_unpullable_default(ref: str | None) -> bool:
    """#1059: True if `ref` must NEVER be the silent wizard default on
    upgrade — a private-ECR host (another account can't pull it; leaks
    an account id) or a digest-pinned ref (`@sha256:…`, frozen — makes
    'upgrade' a no-op). Such a ref stays one keystroke away in Advanced,
    but is never the top default."""
    if not ref:
        return False
    if "@sha256:" in ref:
        return True
    if _PRIVATE_ECR_RE.search(ref):
        return True
    return False


def _public_tag_of(ref: str) -> str | None:
    """The tag of a `public.ecr.aws/<alias>/<repo>:<tag>` ref, else None
    (digest refs and non-public hosts return None)."""
    prefix = f"public.ecr.aws/{PUBLIC_ECR_ALIAS}/{PUBLIC_ECR_REPO}:"
    if ref.startswith(prefix):
        tag = ref[len(prefix):]
        return tag or None
    return None


def upgrade_behind_notice(
    image_from: str | None, default_ref: str | None,
    timeout: float = 4.0,
) -> str | None:
    """#1059: a plain-language notice (or None) for the upgrade image
    question, decided HONESTLY in priority order:
      1. Deployed image is a public-channel tag we can compare to the
         default by digest: equal ⇒ current (None, don't cry wolf);
         differ ⇒ a confident 'out of date' notice.
      2. Deployed image is digest-pinned or private: tg can't order it
         against latest → SOFT copy ('pinned … may be newer'), never a
         false-confident 'behind'.
      3. Can't determine (offline / unresolvable): None (say nothing).
    No account ids / private hosts in any returned string."""
    if not image_from or not default_ref:
        return None
    # (2) digest-pinned or private deployed image — can't order it.
    if is_unpullable_default(image_from):
        return (
            "  You're pinned to a fixed image. The latest published "
            "version may be newer — continuing will switch you to the "
            "latest public image, or keep your current one via "
            "\"Advanced.\"")
    # (1) deployed public tag vs the default — compare by digest.
    dep_tag = _public_tag_of(image_from)
    def_tag = _public_tag_of(default_ref)
    if dep_tag and def_tag:
        dep_dig = public_image_digest(dep_tag, timeout=timeout)
        def_dig = public_image_digest(def_tag, timeout=timeout)
        if dep_dig and def_dig:
            if dep_dig == def_dig:
                return None  # already current — no notice
            return (
                "  Your installed app is out of date. You're running an "
                "older version; continuing will update you to the latest "
                "version. Pick \"keep my current image\" below if you'd "
                "rather stay where you are.")
    # (3) can't tell — stay silent.
    return None


# ── Lockstep image<->CFN-template versioning (#1123) ─────────────────
# Refuse to deploy a CFN template that needs a newer container image
# than the one being deployed — caught at install (before the stack
# create), not at the customer's first query. Two halves:
#   * the IMAGE carries its version as the org.tg.version manifest LABEL
#     (container/Dockerfile) — readable from the registry pre-deploy;
#   * each TEMPLATE carries Metadata.TgMinImageVersion — the minimum
#     image version it requires.
# The installer reads both, compares semver, and refuses on skew
# (warn-fallback only when the image label can't be read — e.g. a
# pre-LABEL image or an offline registry).

_TG_IMAGE_LABEL = "org.tg.version"
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _semver_tuple(v: str):
    """Extract (major, minor, patch) from a version string, ignoring a
    leading 'v' and any -g<sha>/-dirty suffix (so v1.1.0-ga2c3a69 and
    1.1.0 compare equal). Returns None if no semver core is found."""
    if not v:
        return None
    m = _SEMVER_RE.search(v)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def image_label_version(ref: str, timeout: float = 4.0) -> str | None:
    """The org.tg.version LABEL of a public-ECR image, read from the
    registry manifest WITHOUT pulling/running the image, or None if it
    can't be read (pre-LABEL image, digest/private ref, offline). Fetch
    the manifest → its config blob → .config.Labels[org.tg.version].

    None ⇒ 'can't tell' — the caller falls back to a soft warn, never a
    false-confident refusal (ground-by-data)."""
    import json
    import urllib.request

    tag = _public_tag_of(ref)
    if not tag:
        return None  # digest-pinned / private / non-public — can't read
    base = "https://public.ecr.aws"
    repo = f"{PUBLIC_ECR_ALIAS}/{PUBLIC_ECR_REPO}"
    try:
        tok_url = f"{base}/token/?scope=repository:{repo}:pull"
        with urllib.request.urlopen(tok_url, timeout=timeout) as r:
            token = json.load(r).get("token")
        if not token:
            return None

        def _get(url, accept):
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Accept", accept)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    return None
                return json.load(r)

        man = _get(
            f"{base}/v2/{repo}/manifests/{tag}",
            "application/vnd.oci.image.index.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.docker.distribution.manifest.v2+json,"
            "application/vnd.oci.image.manifest.v1+json")
        if not man:
            return None
        # Multi-arch index → follow the first child manifest.
        if "manifests" in man and man.get("manifests"):
            child = man["manifests"][0].get("digest")
            if not child:
                return None
            man = _get(
                f"{base}/v2/{repo}/manifests/{child}",
                "application/vnd.docker.distribution.manifest.v2+json,"
                "application/vnd.oci.image.manifest.v1+json")
            if not man:
                return None
        cfg_digest = (man.get("config") or {}).get("digest")
        if not cfg_digest:
            return None
        cfg = _get(
            f"{base}/v2/{repo}/blobs/{cfg_digest}",
            "application/vnd.oci.image.config.v1+json,"
            "application/vnd.docker.container.image.v1+json,*/*")
        if not cfg:
            return None
        labels = (cfg.get("config") or {}).get("Labels") or {}
        return labels.get(_TG_IMAGE_LABEL)
    except Exception:  # noqa: BLE001 — any failure ⇒ can't tell
        return None


def template_min_image_version(template_path) -> str | None:
    """Read Metadata.TgMinImageVersion from a CFN template, or None if
    absent. A lightweight line-scan (no YAML lib / CFN-tag handling
    needed for one scalar under a known top-level key) keeps this
    dependency-free and robust to the template's !Sub/!Ref tags."""
    try:
        text = Path(template_path).read_text()
    except OSError:
        return None
    in_metadata = False
    for line in text.splitlines():
        if re.match(r"^[A-Za-z]", line):  # a top-level key
            in_metadata = line.startswith("Metadata:")
            continue
        if in_metadata:
            m = re.match(r"\s+TgMinImageVersion:\s*['\"]?([^'\"#\s]+)",
                         line)
            if m:
                return m.group(1)
    return None


def check_image_template_compat(image_ref: str, template_path,
                                timeout: float = 4.0) -> tuple[str, str]:
    """#1123: pre-deploy compatibility gate. Returns (status, message):
      'ok'    — image >= template's min, or the template has no marker
                (nothing to enforce). Proceed.
      'skew'  — the image is OLDER than the template requires. REFUSE:
                deploying would ship templates the image can't serve.
      'warn'  — the image's version LABEL couldn't be read (pre-LABEL
                image / digest ref / offline) AND the template has a
                marker. Can't confirm compat → warn, don't hard-refuse
                (never block a deploy on an unreadable label).
    The caller decides abort vs warn from the status."""
    min_v = template_min_image_version(template_path)
    if not min_v:
        return ("ok", "")
    min_t = _semver_tuple(min_v)
    if not min_t:
        return ("ok", "")  # unparseable marker → don't enforce
    label = image_label_version(image_ref, timeout=timeout)
    img_t = _semver_tuple(label) if label else None
    if img_t is None:
        return (
            "warn",
            f"Could not read the image's version to confirm it supports "
            f"these templates (need >= {min_v}). If the install fails "
            "with a template error, use a newer image "
            f"({PUBLIC_ECR_CHANNEL_TAG}).")
    if img_t < min_t:
        return (
            "skew",
            f"This image (v{label}) is older than the templates require "
            f"(need >= v{min_v}). Deploying would ship reports the app "
            f"can't run. Use a newer image (e.g. the "
            f"{PUBLIC_ECR_CHANNEL_TAG} channel tag) and re-run.")
    return ("ok", "")


# The CFN stack + VPC Name tag tg creates its own VPC under (the
# create-new path). A VPC carrying either is tg-managed — it must
# never be offered as a bring-your-own target, because selecting it
# on a re-run flips tg-container-stack from create-new to BYO mode
# in place, which CloudFormation rejects (the RDS/ALB are already
# live in those subnets) → UPDATE_FAILED + rollback.
TG_MANAGED_STACK_NAME = "tg-container-stack"
TG_MANAGED_VPC_NAME = "tg-vpc"


def vpc_is_tg_managed(vpc: dict) -> bool:
    """True iff this describe-vpcs row is a VPC tg created for its own
    stack — by the CFN stack-name tag or the tg-vpc Name tag. Used to
    keep tg's own VPC out of the BYO pick-list."""
    if not vpc:
        return False
    if vpc.get("stack_name") == TG_MANAGED_STACK_NAME:
        return True
    return vpc.get("name") == TG_MANAGED_VPC_NAME


def list_vpcs(region: str, profile: str | None) -> list[dict]:
    """#774: read-only describe-vpcs → [{id, cidr, name, default,
    stack_name, tg_managed}] for the BYO-VPC pick-list. Returns [] on
    any error (the wizard then falls back to manual id entry).

    Surfaces the `aws:cloudformation:stack-name` tag (and a derived
    `tg_managed` flag) so the wizard can exclude tg's OWN VPC from the
    BYO choices — picking it on a re-run triggers a create-new↔BYO
    mode flip CFN can't do in place."""
    cmd = [
        "aws", "ec2", "describe-vpcs", "--region", region,
        "--query",
        "Vpcs[].{id:VpcId,cidr:CidrBlock,"
        "name:Tags[?Key=='Name']|[0].Value,"
        "stack_name:Tags[?Key=='aws:cloudformation:stack-name']|[0].Value,"
        "default:IsDefault}",
        "--output", "json",
    ]
    if profile:
        cmd += ["--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    import json
    try:
        vpcs = json.loads(proc.stdout) or []
    except json.JSONDecodeError:
        return []
    for v in vpcs:
        v["tg_managed"] = vpc_is_tg_managed(v)
    return vpcs


def _subnet_egress(vpc_id: str, subnet_id: str, region: str,
                   profile: str | None) -> str:
    """#959/#779: classify a subnet's egress by its ROUTE TABLE — the
    SAME way the deploy preflight (_byo_egress_preflight) does, so the
    pick-list label is the attribute the deploy actually gates on
    (MapPublicIpOnLaunch is independent of the route and was the wrong
    signal). Uses the explicit subnet route-table association, falling
    back to the VPC main route table. Returns 'nat' / 'public' /
    'none'. NAT is checked before IGW (a subnet can carry both a
    default IGW and a NAT route; the preflight prefers NAT)."""
    import json

    def _routes(filters):
        cmd = [
            "aws", "ec2", "describe-route-tables", "--region", region,
            "--filters", *filters,
            "--query", "RouteTables[].Routes[].{g:GatewayId,n:NatGatewayId}",
            "--output", "json",
        ]
        if profile:
            cmd += ["--profile", profile]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return []
        try:
            return json.loads(p.stdout) or []
        except json.JSONDecodeError:
            return []

    routes = _routes([f"Name=association.subnet-id,Values={subnet_id}"])
    if not routes:
        # No explicit association → the VPC main route table applies.
        routes = _routes([f"Name=vpc-id,Values={vpc_id}",
                          "Name=association.main,Values=true"])
    blob = " ".join(
        f"{r.get('g') or ''} {r.get('n') or ''}" for r in routes).lower()
    if "nat-" in blob:
        return "nat"
    if "igw-" in blob:
        return "public"
    return "none"


def vpc_interface_endpoint_count(vpc_id: str, region: str,
                                 profile: str | None) -> int:
    """#959/#779: count the 4 interface endpoints the Fargate task needs
    to start in an all-private (no-NAT) VPC — secretsmanager + ecr.api +
    ecr.dkr + logs. Mirrors the preflight's _byo_required_endpoints so
    the wizard can allow an all-'none' subnet set ONLY when they exist.
    Returns 0 on any error (caller treats <4 as 'not satisfied')."""
    services = ",".join(
        f"'com.amazonaws.{region}.{s}'"
        for s in ("secretsmanager", "ecr.api", "ecr.dkr", "logs"))
    cmd = [
        "aws", "ec2", "describe-vpc-endpoints", "--region", region,
        "--filters", f"Name=vpc-id,Values={vpc_id}",
        "Name=vpc-endpoint-type,Values=Interface",
        "--query",
        f"length(VpcEndpoints[?contains([{services}], ServiceName)])",
        "--output", "text",
    ]
    if profile:
        cmd += ["--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip())
    except (TypeError, ValueError):
        return 0


def list_subnets(vpc_id: str, region: str, profile: str | None) -> list[dict]:
    """#774: read-only describe-subnets for a VPC → [{id, az, cidr,
    public, egress}] for the subnet pick-list. Returns [] on any error.

    #959: `egress` ('public'/'nat'/'none') is the ROUTE-based class the
    deploy preflight gates on (see _subnet_egress). The legacy `public`
    field (MapPublicIpOnLaunch) is kept for back-compat but is NOT the
    deploy contract — the wizard labels + the homogeneity guard key off
    `egress`."""
    cmd = [
        "aws", "ec2", "describe-subnets", "--region", region,
        "--filters", f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "Subnets[].{id:SubnetId,az:AvailabilityZone,"
        "cidr:CidrBlock,public:MapPublicIpOnLaunch}",
        "--output", "json",
    ]
    if profile:
        cmd += ["--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    import json
    try:
        subs = json.loads(proc.stdout) or []
    except json.JSONDecodeError:
        return []
    for s in subs:
        s["egress"] = _subnet_egress(vpc_id, s["id"], region, profile)
    return subs


def list_acm_certs(region: str, profile: str | None) -> list[dict]:
    """#988: read-only `aws acm list-certificates` → [{arn, domain}] of
    the ISSUED certs in `region`, for the wizard's existing-cert
    pick-list (so the operator picks by domain instead of hand-typing an
    opaque ARN). Returns [] on any error (the wizard then falls back to
    manual ARN entry — same defensive shape as list_vpcs).

    Two gotchas, both in the query:
    - `--certificate-statuses ISSUED` — only usable certs (never offer a
      PENDING_VALIDATION/EXPIRED cert the deploy would then reject).
    - `--includes keyTypes=...` is MANDATORY: list-certificates defaults
      to RSA_2048 ONLY, so an ECDSA / RSA_4096 ALB cert is silently
      invisible without an explicit keyTypes filter (the subtle bug that
      would make the menu 'lose' a cert the operator knows exists)."""
    cmd = [
        "aws", "acm", "list-certificates", "--region", region,
        "--certificate-statuses", "ISSUED",
        "--includes",
        "keyTypes=RSA_1024,RSA_2048,RSA_3072,RSA_4096,"
        "EC_prime256v1,EC_secp384r1,EC_secp521r1",
        "--query",
        "CertificateSummaryList[].{arn:CertificateArn,domain:DomainName}",
        "--output", "json",
    ]
    if profile:
        cmd += ["--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    import json
    try:
        return json.loads(proc.stdout) or []
    except json.JSONDecodeError:
        return []


def caller_account(profile: str | None = None) -> str | None:
    """#874: the AWS account of the current caller, via read-only
    `aws sts get-caller-identity`. Used to SUGGEST TG_TARGET_ACCOUNT_ID
    in the wizard when it's unset (instead of falling through to a late
    pre-flight hard-fail). ~/.aws/config is NOT a reliable source — the
    default credential chain / instance role / env creds carry no
    account — so STS is the only dependable live signal (same call the
    installer's pre-flight uses). Returns None on any error; the caller
    then just prompts without a default."""
    cmd = ["aws", "sts", "get-caller-identity",
           "--query", "Account", "--output", "text"]
    if profile:
        cmd += ["--profile", profile]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    acct = proc.stdout.strip()
    return acct if acct.isdigit() and len(acct) == 12 else None


def _profile_is_sso(profile: str | None) -> bool:
    """True if the resolved profile is SSO-based (carries sso_session
    or sso_start_url). Best-effort: a profile that reaches SSO only via
    source_profile (role-chain) may report False — the universal
    liveness probe in preflight_caller still catches an expired session
    regardless; only the SSO-specific remediation message is skipped
    (#1087 OQ1, accepted)."""
    for key in ("sso_session", "sso_start_url"):
        cmd = ["aws", "configure", "get", key]
        if profile:
            cmd += ["--profile", profile]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    return False


def profile_not_found(profile: str | None) -> bool:
    """#1093: True iff AWS_PROFILE is SET but does not resolve — the
    profile name isn't in `~/.aws/config` (a typo, or a name that
    doesn't exist). Distinguished from "profile exists but its session
    is expired/invalid" (which #1087's preflight already handles): a
    not-found profile must abort up front naming the bad name, NOT
    silently fall through to the account prompt.

    Detection uses `aws configure list-profiles` (the authoritative
    list) rather than stderr-string matching, so it's locale- and
    CLI-version-independent. Returns False for an unset profile (the
    default-chain path is valid — never a not-found error) and False if
    we can't enumerate profiles at all (fail open: let the downstream
    liveness probe report it rather than a false not-found abort)."""
    if not profile:
        return False
    cmd = ["aws", "configure", "list-profiles"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False   # can't list → don't claim not-found
    if proc.returncode != 0:
        return False
    profiles = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
    # Only assert not-found when we got a usable list that omits it.
    return bool(profiles) and profile not in profiles


def _path_python3_version() -> tuple[int, int] | None:
    """The (major, minor) of the `python3` on PATH — the interpreter the
    bash installers actually invoke (they call bare `python3` for JSON
    parsing, since jq isn't guaranteed). This MAY differ from the
    interpreter running this CLI (e.g. `tg` launched via a venv while the
    scripts resolve a system python3), so the gate must check the one the
    SCRIPTS use, not just `sys.version_info`. Returns None if PATH
    `python3` can't be run (then the caller falls back to sys)."""
    cmd = ["python3", "-c",
           "import sys;print('%d %d' % sys.version_info[:2])"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def check_python(min_ver: tuple[int, int] = TG_MIN_PYTHON) -> tuple[bool, str]:
    """#1115: gate on the minimum supported Python BEFORE any install
    step, so a too-old interpreter fails fast with an upgrade message
    instead of a cryptic mid-install SyntaxError/ImportError deep in a
    `python3` subprocess.

    Checks the PATH `python3` the bash scripts will invoke (via
    _path_python3_version); falls back to this process's
    `sys.version_info` only if PATH `python3` can't be probed. Returns
    (ok, detected) where detected is "MAJOR.MINOR"; `ok` False means the
    install MUST NOT proceed. Pure introspection — no AWS, no side
    effects — so it's unit-testable with a monkeypatched version (the
    #1087 seam pattern)."""
    ver = _path_python3_version() or sys.version_info[:2]
    cur = (ver[0], ver[1])
    detected = "%d.%d" % cur
    return (cur >= tuple(min_ver), detected)


def python_upgrade_message(detected: str,
                           min_ver: tuple[int, int] = TG_MIN_PYTHON) -> str:
    """The actionable abort text when check_python fails — names the
    required vs detected version and gives a platform-appropriate hint
    (#1115 OQ2: macOS has no default system python3, so mention BOTH
    Homebrew and the python.org installer; Linux → the distro package)."""
    need = "%d.%d" % tuple(min_ver)
    return (
        f"Python {need}+ is required to run tg install; "
        f"detected {detected}.\n"
        f"  Upgrade python3 and re-run:\n"
        f"    macOS:  brew install python@{need}   "
        f"(or the installer from python.org)\n"
        f"    Linux:  install your distro's python3 "
        f"package (apt/dnf/…)"
    )


def _classify_docker_error(text: str) -> str:
    """Map a docker stderr blob to a cause token: 'signin' (Docker
    Desktop org/sign-in policy blocks BUILD even though the daemon
    answers), 'no_docker' (binary absent), 'daemon_down' (daemon
    unreachable), or 'build_failed' (some other build failure). Pure +
    unit-testable — the build-capability preflight classifies on this."""
    t = (text or "").lower()
    if ("sign in" in t or "sign-in" in t or "membership in the" in t
            or "organization is required" in t
            or "enforced by your administrator" in t):
        return "signin"
    if ("not found" in t and "docker" in t) or "no such file" in t \
            or "command not found" in t:
        return "no_docker"
    if ("cannot connect to the docker daemon" in t
            or "is the docker daemon running" in t
            or "daemon not reachable" in t):
        return "daemon_down"
    return "build_failed"


def docker_build_preflight(timeout: float = 60.0) -> tuple[bool, str]:
    """When the operator chooses build-from-source, confirm Docker
    can actually BUILD — not just that the daemon answers. `docker info`
    SUCCEEDS even when Docker Desktop's build is org-sign-in/policy
    blocked, so the install used to die mid-build after the stack was
    partly up. This runs a trivial throwaway build (`docker buildx build
    --output type=cacheonly` of an inline `FROM scratch`) that exercises
    the SAME build path the policy gates, builds nothing persistent, and
    adds only seconds.

    Returns (ok, message). ok=True → silent proceed (message ''). ok=False
    → the install MUST stop; message is the specific cause + fix + the
    prebuilt-image offer. Pure-ish (only spawns docker) so tests mock the
    subprocess. Any probe error is treated as a build failure (fail
    closed — never proceed to a real build we couldn't validate)."""
    if shutil.which("docker") is None:
        return (False, _docker_fix_message("no_docker"))
    # buildx + an inline Dockerfile via stdin; cacheonly keeps nothing.
    cmd = ["docker", "buildx", "build", "--output", "type=cacheonly", "-"]
    try:
        proc = subprocess.run(
            cmd, input="FROM scratch\n",
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return (False, _docker_fix_message("no_docker"))
    except (OSError, subprocess.SubprocessError):
        # timeout / spawn failure → can't validate → fail closed.
        return (False, _docker_fix_message("build_failed"))
    if proc.returncode == 0:
        return (True, "")
    cause = _classify_docker_error(
        (proc.stderr or "") + "\n" + (proc.stdout or ""))
    return (False, _docker_fix_message(cause))


def _docker_fix_message(cause: str) -> str:
    """Actionable abort text per docker cause — always offers the
    prebuilt public image (no Docker) as the escape, since that's the
    one-keystroke fix for an unbuildable Docker."""
    prebuilt = ("  Or choose the prebuilt public image at the container "
                "step (option 1 — no Docker needed).")
    if cause == "signin":
        return (
            "Docker can't build — Docker Desktop requires sign-in (an org "
            "policy enforced by your administrator). Open Docker Desktop "
            "and sign in with your organization account, then re-run "
            "`tg install`.\n" + prebuilt)
    if cause == "no_docker":
        return (
            "Docker is not installed (or not on PATH) — building from "
            "source needs it. Install Docker, then re-run `tg install`.\n"
            + prebuilt)
    if cause == "daemon_down":
        return (
            "The Docker daemon isn't reachable — start Docker (open Docker "
            "Desktop / `systemctl start docker`), then re-run "
            "`tg install`.\n" + prebuilt)
    return (
        "Docker can't build a trivial image — `docker buildx build` "
        "failed. Check `docker buildx build` works locally, then re-run "
        "`tg install`.\n" + prebuilt)


def preflight_caller(env: dict) -> dict:
    """#1087: resolve + validate the AWS credential source BEFORE any
    install step, so a stale/absent session fails up front with clear
    remediation instead of a cryptic mid-install error.

    AWS_PROFILE is OPTIONAL (the #768 pattern): when set we pin it,
    else we use the default credential chain. The single read-only
    `aws sts get-caller-identity` is the universal liveness probe — it
    catches an expired/invalid session for ANY credential type (SSO,
    env creds, instance role), per OQ1/OQ2.

    Returns a dict the caller (wizard) acts on:
      {"ok": bool, "source": str, "profile": str|None,
       "is_sso": bool, "account": str|None, "arn": str|None}
    `ok` False means the install MUST NOT proceed.
    """
    import json

    profile = (env or {}).get("AWS_PROFILE") or os.environ.get("AWS_PROFILE")
    profile = profile or None
    if profile:
        source = f"profile {profile}"
    else:
        source = ("default credential chain "
                  "(instance role / SSO default / env creds)")
    is_sso = _profile_is_sso(profile)

    cmd = ["aws", "sts", "get-caller-identity",
           "--query", "{Account:Account,Arn:Arn}", "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    account = arn = None
    ok = False
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            try:
                ident = json.loads(proc.stdout)
                account = ident.get("Account")
                arn = ident.get("Arn")
                ok = bool(account and arn)
            except json.JSONDecodeError:
                ok = False
    except (OSError, subprocess.SubprocessError):
        ok = False

    return {
        "ok": ok, "source": source, "profile": profile,
        "is_sso": is_sso, "account": account, "arn": arn,
    }


def describe_stack(stack: str, region: str, profile: str | None) -> dict | None:
    """Read-only: return key stack facts for `tg status`, or None."""
    cmd = [
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", stack, "--region", region,
        "--query", "Stacks[0].{Status:StackStatus,Outputs:Outputs}",
        "--output", "json",
    ]
    if profile:
        cmd += ["--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    import json

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def stack_output(stack: str, key: str, region: str,
                 profile: str | None) -> str | None:
    """#1000: read a single CFN stack Output value (e.g. AlbDnsName),
    or None on any error / missing key. Used to find the deployed app's
    URL for the end-of-install /api/version match-check."""
    info = describe_stack(stack, region, profile)
    if not info:
        return None
    for o in (info.get("Outputs") or []):
        if o.get("OutputKey") == key:
            v = o.get("OutputValue")
            return v or None
    return None


CONTAINER_STACK = "tg-container-stack"

# #962: CFN Parameter key → wizard answer key. ONE explicit, auditable
# table — the deployed stack's params seed the wizard defaults on an
# upgrade so Enter-through reproduces the live config (and an upgrade is
# the path of least resistance, instead of a generic default flipping
# create-new↔BYO → rollback, the #961 footgun). Only NON-secret,
# CFN-visible params map here (the OIDC secret is in Secrets Manager,
# never a CFN param). Ingress CIDRs ARE top-level params
# (AllowedIngressCidr1..4), so they read back too (joined → the
# comma-form the wizard/validator use). IAM prefix is baked into
# resource names at create, not a CFN param — immutable on upgrade, so
# it's not mapped (locked to its default, OQ2).
_CFN_PARAM_TO_ANSWER = {
    # #1059: EcsImageUri is intentionally NOT mapped to "image". The
    # deployed image flows out as image_from (→ _image_from) for the
    # banner + Advanced prefill + behind-check, but must NOT seed
    # answers["image"] — doing so shadowed the #877 public-image default
    # and re-offered the stale/private deployed digest on upgrade.
    "ExistingVpcId": "vpc_id",
    "ExistingSubnetIds": "subnet_ids",
    "BootstrapAdminEmail": "bootstrap_email",
    "CertificateArn": "cert_arn",
    "DomainName": "domain_name",
    "OidcIssuer": "oidc_issuer",
    "OidcClientId": "oidc_client_id",
}


def stack_parameters(stack: str, region: str,
                     profile: str | None) -> dict:
    """#962: read a deployed stack's CFN Parameters → {ParameterKey:
    ParameterValue}. Returns {} on any error / no stack (so the
    greenfield + no-perms paths stay byte-identical to today — the
    caller treats {} as 'no deployed stack to upgrade'). CFN never
    returns NoEcho values, so this exposes only non-secret config."""
    import json
    cmd = [
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", stack, "--region", region,
        "--query", "Stacks[0].Parameters",
        "--output", "json",
    ]
    if profile:
        cmd += ["--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {}
    try:
        params = json.loads(proc.stdout) or []
    except json.JSONDecodeError:
        return {}
    return {
        p["ParameterKey"]: p.get("ParameterValue", "")
        for p in params if "ParameterKey" in p
    }


# CFN stack statuses that can accept an in-place update. Anything else
# (*_IN_PROGRESS, ROLLBACK_FAILED, *_FAILED) is refused with guidance —
# attempting an update CFN will reject just adds a confusing failure.
_UPDATABLE_STATUSES = {
    "CREATE_COMPLETE", "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_COMPLETE",
}


def deployed_stack_defaults(
    region: str, profile: str | None
) -> dict | None:
    """#962: detect a deployed tg-container-stack and return upgrade
    context, or None when there's no stack (greenfield — unchanged).

    Returns {
      "status": <StackStatus>,
      "updatable": bool,             # status can accept an update
      "answers": {answer_key: value},# deployed params → wizard defaults
      "image_from": <EcsImageUri>,   # for the from→to confirm banner
      "vpc_mode_create_new": bool,   # ExistingVpcId empty ⇒ create-new
    }
    Degrades to None on any describe error (no stack / no perms), so the
    install falls back to today's new-install defaults."""
    info = describe_stack(CONTAINER_STACK, region, profile)
    if not info or not info.get("Status"):
        return None
    params = stack_parameters(CONTAINER_STACK, region, profile)
    answers: dict = {}
    for cfn_key, ans_key in _CFN_PARAM_TO_ANSWER.items():
        if cfn_key in params and params[cfn_key] != "":
            answers[ans_key] = params[cfn_key]
    # Ingress CIDRs: AllowedIngressCidr1..4 → the comma-form the wizard
    # validator (V.cidrs) expects. Skip empty slots.
    cidrs = [
        params.get(f"AllowedIngressCidr{i}", "") for i in range(1, 5)]
    cidrs = [c for c in cidrs if c]
    if cidrs:
        answers["ingress_cidrs"] = ",".join(cidrs)
    # RequireLogin (true/false string) → enable_login (y/n).
    rl = params.get("RequireLogin", "")
    if rl:
        answers["enable_login"] = "y" if rl.lower() == "true" else "n"
    vpc_mode_create_new = not params.get("ExistingVpcId", "")
    return {
        "status": info["Status"],
        "updatable": info["Status"] in _UPDATABLE_STATUSES,
        "answers": answers,
        "image_from": params.get("EcsImageUri", ""),
        "vpc_mode_create_new": vpc_mode_create_new,
    }
