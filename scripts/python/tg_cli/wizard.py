"""The install wizard + cert 3-way + confirm screen (#487).

Collects answers (resuming from ~/.tg/config.json / env), then maps
them to the TG_* environment contract that scripts/tg-ecs-install.sh
consumes. Owns no deploy logic — it only gathers + validates + maps.

The questions:
  1. region
  2. endpoint ingress CIDR(s)
  3. container image (build from source, or an existing ECR URI)
  4. IAM names (accept the tg- defaults, or override)
  5. emails (bootstrap admin + alert)
VPC / DB are shown on the confirm screen (not asked — always created).
#1075: CUR / cost reporting is NOT a question — it's a required step
(the sole spend source), surfaced as an informational notice only.
The cert choice (3-way) rides on the endpoint/confirm stage.
#725 (#720 slice 3): the Bedrock-invocation-log-group question is
gone — CUR is the sole spend source; tg enables no invocation log.
"""
from __future__ import annotations

from . import validate as V
from .prompts import Question, Resolver

# #903: plain-language choice labels — no bare "SG" / "ACM ARN"
# acronyms; outcome (HTTP/HTTPS) stated first. The ARN detail stays
# in the cert_arn follow-up question's `why` (which already explains
# AWS Certificate Manager + where to find an ARN). These values are
# compared via the CONSTANTS everywhere (never string literals), so
# rewording is safe; the confirm screen's TLS: line prints the value
# verbatim and picks up the new wording automatically.
CERT_EXISTING = (
    "HTTPS with my own certificate "
    "(I already have one in AWS Certificate Manager)")
CERT_SELFSIGNED = "Generate a self-signed cert now (HTTPS; browser warning)"
CERT_PLAINTEXT = (
    "Plain HTTP, no encryption "
    "(reachable only from your allowed IPs; must opt in)")

# #988: the existing-cert pick-list's manual-entry escape (mirrors
# IMAGE_CUSTOM). Selecting it falls through to the hand-typed ARN
# Question — the path for a cert the list didn't surface, or no creds.
CERT_ARN_MANUAL = "Enter an ARN manually"


def _cert_label(domain: str, arn: str) -> str:
    """#988: a pick-list label for an ACM cert — domain first (what the
    operator recognizes), the ARN's last segment to disambiguate
    same-domain certs."""
    tail = (arn or "").rsplit("/", 1)[-1]
    return f"{domain or '(no domain)'} — {tail}"

# #774: VPC modes.
VPC_CREATE = "Create a new VPC (default — greenfield)"
VPC_EXISTING = "Use an existing VPC (required if at the VPC limit)"

# #796 (#782): admin auth provider. Cognito is the always-on base
# login — the installer stands up tg-cognito-pool and derives the
# OIDC values, so NO Okta tenant is needed. Okta is the opt-in
# bring-your-own-OIDC path (then the TG_OIDC_* trio is required).
AUTH_COGNITO = "Cognito (default — tg stands up the login, no Okta needed)"
AUTH_OKTA = "Okta / bring-your-own OIDC issuer"


def _q_region(d):
    return Question(
        key="region",
        prompt="AWS region",
        why="WHY: where the stack deploys. CUR 2.0 only operates in "
        "us-east-1, so the pilot pins there.",
        default=d.get("region", "us-east-1"),
        validate=V.region,
    )


# Bedrock invocation-logging (analytics capture) region set. Default-ON
# (seeded with the install region); skippable. Separate from CUR spend —
# this captures per-invocation prompt/token data for a future
# model-selection recommender. Multi-region because a customer may run
# coding tools in several regions and the logging config is per-region.
INVLOGS_ON = "Yes — capture in my install region (recommended)"
INVLOGS_CUSTOM = "Yes — pick the region(s) my coding tools use"
INVLOGS_SKIP = "Skip — don't capture invocation logs"


def _q_invlogs_regions(d):
    return Question(
        key="invlogs_regions",
        prompt="Which region(s) do your coding tools call Bedrock in?",
        why="WHY: enables prompt/token analytics (for future "
        "model-selection tuning) by logging Bedrock invocations to "
        "encrypted S3 in each region. Separate from cost tracking; "
        "captures prompt + response text. Comma-separate regions, or "
        "leave blank to skip. You can change this later in Settings.",
        default=d.get("invlogs_regions", d.get("region", "us-east-1")),
        validate=V.regions,
    )


def _ask_invlogs(resolver: Resolver, answers: dict) -> dict:
    """The invocation-logging capture regions. A pre-supplied value
    (env/config/scripted/--non-interactive) flows straight through the
    validated question — default-ON with the install region. Interactive
    + no pre-set value gets a small choice set: capture-here (default) /
    pick-regions / skip, so the privacy-relevant Text capture is an
    explicit opt-in-by-default the operator sees, never silent."""
    # Pre-supplied / non-interactive → the manual question (validated),
    # default = the install region (capture-on-by-default).
    if not resolver.interactive or "invlogs_regions" in resolver.supplied \
            or (resolver.scripted is not None
                and "invlogs_regions" in resolver.scripted):
        answers["invlogs_regions"] = resolver.ask(_q_invlogs_regions(answers))
        return answers
    install_region = answers.get("region", "us-east-1")
    choice = resolver.ask(Question(
        key="invlogs_choice",
        prompt="Capture Bedrock invocation logs for analytics?",
        why="WHY: logs prompt/token data to encrypted S3 (per region) "
        "for future model-selection tuning — separate from cost "
        "tracking, and it captures prompt + response text. Default on "
        "for your install region; skippable; editable later in Settings.",
        default=INVLOGS_ON,
        choices=[INVLOGS_ON, INVLOGS_CUSTOM, INVLOGS_SKIP],
    ))
    if choice == INVLOGS_SKIP:
        answers["invlogs_regions"] = ""
    elif choice == INVLOGS_CUSTOM:
        answers["invlogs_regions"] = resolver.ask(_q_invlogs_regions(answers))
    else:  # INVLOGS_ON
        answers["invlogs_regions"] = install_region
    return answers


def _q_cidrs(d):
    return Question(
        key="ingress_cidrs",
        prompt="Allowed ingress CIDR(s), comma-separated",
        why="WHY: which public IP(s) may reach the admin web console. "
        "Enter the IP you'll browse from in CIDR form — a single IP is "
        "/32 (e.g. 203.0.113.7/32). Find yours at "
        "https://checkip.amazonaws.com. Comma-separate up to 4 (e.g. "
        "office + home). 0.0.0.0/0 (open to the whole internet) is "
        "allowed only with the login wall on (and TG_REQUIRE_IP_ALLOWLIST "
        "off); otherwise rejected. You can change this later.",
        default=d.get("ingress_cidrs"),
        validate=V.cidrs,
    )


# #875: ingress-CIDR choice set offered interactively. The manual
# question above is the fallback (no egress) + non-interactive path.
CIDR_CUSTOM = "Custom CIDR(s) — office / home / VPN (up to 4)"
CIDR_OPEN_ALL = "Open to all 0.0.0.0/0 (login wall is your only barrier)"


def _cidr_detected_label(ip: str) -> str:
    return f"Detected current IP {ip}/32 (recommended)"


def _ask_cidrs(resolver: Resolver, answers: dict) -> dict:
    """#875: the ALB ingress allowlist. A pre-supplied value (env /
    config / scripted / --non-interactive) flows straight through the
    manual question (validated) — byte-identical to the old behavior.
    Interactive + no pre-set value gets a small choice set:
      1. detected current public IP as a /32 (default, when egress works),
      2. custom CIDR(s) — today's manual entry,
      3. open-to-all 0.0.0.0/0 — offered ONLY when the login wall is on
         and TG_REQUIRE_IP_ALLOWLIST is off, with an inline warning.
    Graceful: no egress / detect-fail → fall back to manual entry.
    """
    # Pre-supplied (env/config/scripted) or non-interactive → the manual
    # question, which the resolver answers from supplied/scripted without
    # prompting. Byte-identical to the pre-#875 single-question path.
    pre = (
        answers.get("ingress_cidrs")
        or resolver.supplied.get("ingress_cidrs")
        or (resolver.scripted or {}).get("ingress_cidrs")
    )
    if pre or not resolver.interactive:
        answers["ingress_cidrs"] = resolver.ask(_q_cidrs(answers))
        return answers

    from . import runner
    detected = runner.detect_public_ip()

    # open-all is offered only when login is on AND the strict-allowlist
    # policy is off — mirrors validate.cidrs / tg-ecs-install.sh exactly.
    login_on = V.is_yes(answers.get("enable_login", "y"))
    allow_open = login_on and not V.require_ip_allowlist()

    choices = []
    if detected:
        choices.append(_cidr_detected_label(detected))
    choices.append(CIDR_CUSTOM)
    if allow_open:
        choices.append(CIDR_OPEN_ALL)

    # Nothing to choose between (no detection, open-all not offered) →
    # skip the one-item menu and ask the manual question directly.
    if choices == [CIDR_CUSTOM]:
        answers["ingress_cidrs"] = resolver.ask(_q_cidrs(answers))
        return answers

    pick = resolver.ask(
        Question(
            key="ingress_choice",
            prompt="Who can reach the ALB endpoint?",
            why="WHY: the ALB is the only ingress. Lock it to your "
            "office/VPN, or open it and rely on the login wall.",
            default=choices[0],
            choices=choices,
        )
    )
    if detected and pick == _cidr_detected_label(detected):
        answers["ingress_cidrs"] = f"{detected}/32"
    elif allow_open and pick == CIDR_OPEN_ALL:
        resolver.note(
            "0.0.0.0/0 — anyone can reach the console; the Cognito/OIDC "
            "login wall is your only barrier. Ensure a strong IdP."
        )
        answers["ingress_cidrs"] = "0.0.0.0/0"
    else:
        answers["ingress_cidrs"] = resolver.ask(_q_cidrs(answers))
    return answers


def _q_image(d):
    return Question(
        key="image",
        prompt="Container image: a prebuilt image URI, or 'build' from source",
        why="WHY: how to get the app's container image. 'build' builds it "
        "from this repo and pushes to a private ECR the stack creates "
        "(needs Docker locally); or paste a prebuilt image URI (e.g. the "
        "public image) to skip the build entirely. Default: build.",
        default=d.get("image", "build"),
    )


# #877: container-image choice set offered interactively. The manual
# question above is the fallback (no resolvable prebuilt image) +
# non-interactive path.
IMAGE_BUILD = "Build from source (needs Docker; builds + pushes to a private ECR)"
IMAGE_CUSTOM = "Advanced: paste an existing ECR image URI"


def _image_prebuilt_label(ref: str) -> str:
    return f"Pull prebuilt public image — {ref} (recommended; no Docker)"


def _ask_image(resolver: Resolver, answers: dict) -> dict:
    """#877: how to get the container image. A pre-supplied value (env /
    config / scripted / --non-interactive) flows straight through the
    manual question — byte-identical to the old behavior. Interactive +
    no pre-set value gets a 3-way choice with the prebuilt PUBLIC image
    as the recommended default (no Docker), pinned to the version
    matching this checkout:
      1. Pull prebuilt public image (default) — public.ecr.aws/<alias>/
         tg-container:<version> (or the channel tag), probed pullable.
      2. Build from source ('build') — today's path; needs Docker.
      3. Advanced: paste an existing ECR image URI.
    Graceful: if NO prebuilt image is resolvable (offline / pipeline not
    yet run / 404), the prebuilt option is omitted and the default falls
    back to 'build' — the wizard never suggests an unpullable ref.
    """
    from . import runner

    # #1059: distinguish OPERATOR-supplied (env / --non-interactive /
    # scripted / explicit config — honor byte-for-byte) from an
    # UPGRADE-detected image (deployed off the live stack — flows in as
    # `_image_from`, must NOT silently become the default). Note
    # answers["image"] is deliberately NOT consulted here: the #962
    # upgrade prefill no longer seeds it (#1059), so the only path that
    # sets it pre-prompt is genuine operator supply, captured below.
    pre = (
        resolver.supplied.get("image")
        or (resolver.scripted or {}).get("image")
    )
    if pre or not resolver.interactive:
        # Operator supplied it (or non-interactive) → honor exactly.
        if pre and not answers.get("image"):
            answers["image"] = pre
        # #1059: a non-interactive UPGRADE with NO operator-supplied
        # image must default to the newest public image (or build),
        # never the deployed private/stale digest (which isn't even
        # seeded here any more). Resolve it so the manual question's
        # default isn't a bare "build" that silently rebuilds.
        if (not pre and not answers.get("image")
                and answers.get("_is_upgrade")):
            answers["image"] = runner.resolve_upgrade_image() or "build"
        answers["image"] = resolver.ask(_q_image(answers))
        return answers

    is_upgrade = bool(answers.get("_is_upgrade"))
    image_from = answers.get("_image_from") or ""

    # The recommended default. On upgrade: the NEWEST published public
    # image (latest), so the operator actually moves forward — never the
    # deployed (possibly stale/private/digest) ref. Greenfield: the #877
    # version-pinned prebuilt. Either way it's probed pullable.
    if is_upgrade:
        prebuilt = runner.resolve_upgrade_image()
        # The detected deployed image pre-fills the Advanced path so
        # "keep what I have" is one keystroke — but only when it's a
        # sane value to re-offer (not used as the top default regardless).
        custom_prefill = image_from or None
        # Behind / pinned notice — honest, shown before the question.
        notice = runner.upgrade_behind_notice(image_from, prebuilt)
        if notice:
            print(notice)
    else:
        prebuilt = runner.resolve_prebuilt_image()
        custom_prefill = answers.get("image")
        # Advisory only (fail-silent): hint if the public channel tag has
        # moved past the version pinned to this checkout. Never blocks;
        # any probe error / offline → no hint. The customer can't fix a
        # stale public image, but the owner wants the signal in-flow.
        try:
            _hint = runner.newer_public_image_hint()
        except Exception:  # noqa: BLE001 — advisory must never raise
            _hint = None
        if _hint:
            print(_hint)

    choices = []
    if prebuilt:
        choices.append(_image_prebuilt_label(prebuilt))
    choices.append(IMAGE_BUILD)
    choices.append(IMAGE_CUSTOM)

    pick = resolver.ask(
        Question(
            key="image_choice",
            prompt="Container image — pull a prebuilt one, or build?",
            why="WHY: the prebuilt public image needs no Docker and is "
            "pinned to this version for a reproducible install. 'Build' "
            "compiles from this repo (needs Docker). Or paste your own "
            "image URI.",
            default=choices[0],
            choices=choices,
        )
    )
    if prebuilt and pick == _image_prebuilt_label(prebuilt):
        answers["image"] = prebuilt
    elif pick == IMAGE_CUSTOM:
        answers["image"] = resolver.ask(Question(
            key="image",
            prompt="ECR image URI",
            why="WHY: the api/worker pull this exact image; the stack "
            "skips the build.",
            default=custom_prefill,
        ))
    else:
        answers["image"] = "build"
    return answers


def _q_iam_names(d):
    return Question(
        key="iam_prefix",
        prompt="IAM resource naming prefix",
        why="WHY: the tg- prefix on the roles/policies the stack creates. "
        "Accept the default unless your org mandates another prefix.",
        default=d.get("iam_prefix", "tg-"),
    )


def _q_bootstrap_email(d):
    return Question(
        key="bootstrap_email",
        prompt="Bootstrap admin email (seeded as org_admin)",
        why="WHY: the email that becomes the first org_admin — you sign "
        "into the admin web console with it and set up everyone else from "
        "there. On Cognito (the default), the admin is pre-confirmed at "
        "install: either you set a password now (next question), or you "
        "use 'Forgot password' on the login page (a reset code is emailed "
        "here) to set yours. On Okta/OIDC, you sign in through your IdP as "
        "this user. Use a real mailbox you can access.",
        default=d.get("bootstrap_email"),
        validate=V.email,
    )


def _q_bootstrap_password(d):
    return Question(
        key="bootstrap_password",
        prompt="Admin password (Enter to skip — random + forgot-password)",
        why="WHY: optionally set the first admin's password now so you can "
        "sign in immediately. Leave it BLANK to have the installer set a "
        "random throwaway (never shown) and confirm the account — then sign "
        "in via 'Forgot password' to set your own. Either way the admin is "
        "pre-confirmed so forgot-password works. (Cognito only; ignored on "
        "Okta/OIDC.) Min 12 chars incl. lower, upper, and a number. This is "
        "a SECRET — it is never saved to disk or logged.",
        default="",
        validate=V.admin_password,
        secret=True,
    )


# Ordered question set. Cert is handled separately (a 3-way).
# Notification transport (SMTP relay + optional webhook) is NOT a
# wizard question — it's configured post-install in the Settings UI
# (Notifications) so an admin can manage the secrets there rather than
# threading them through install env.
# CUR cost reporting is NOT a question: it's the sole spend +
# per-user-attribution source, so the customer has no meaningful
# choice — it's surfaced as an informational notice (run_questions),
# not a y/n that implies an option that doesn't exist.
QUESTION_BUILDERS = [
    _q_region,
    _q_cidrs,
    _q_image,
    _q_iam_names,
    _q_bootstrap_email,
    _q_bootstrap_password,
]

# #530 phase 2: the docker-compose --local path uses NONE of the
# ALB/ECR questions (ingress CIDRs, container image URI, IAM
# prefix) — tg-local-install.sh doesn't read TG_ALLOWED_INGRESS_
# CIDRS / EcsImageUri / a prefix. Asking them (and failing
# --non-interactive when they're unset) was a bug.
# Reduced set: region, log group, CUR, emails. No cert 3-way
# either (compose has no ALB/TLS termination). OIDC/login still
# applies (the api gates the SPA regardless of compose-vs-ECS).
LOCAL_QUESTION_BUILDERS = [
    _q_region,
    _q_bootstrap_email,
    _q_bootstrap_password,
]


def run_questions(resolver: Resolver, answers: dict, local: bool = False) -> dict:
    """Ask the install questions in order, mutating + returning
    `answers`. `local=True` uses the reduced docker-compose set and
    skips the ECS-only cert 3-way (#530 phase 2).

    `answers` seeds defaults (from config/env) and accumulates results
    so the caller can persist after each step for resume.
    """
    builders = LOCAL_QUESTION_BUILDERS if local else QUESTION_BUILDERS
    for build in builders:
        q = build(answers)
        # CUR cost reporting is not a choice — surface it as an
        # informational notice in the spot the old y/n question ran
        # (just before the emails), so the flow still reads naturally.
        # #1075: CUR is REQUIRED (the sole spend source); there is no
        # opt-out, so the notice states it plainly as a standard step.
        if q.key == "bootstrap_email":
            resolver.note(
                "CUR 2.0 cost reporting is deployed as a standard, "
                "required step — it's the sole source tg uses to "
                "attribute per-user $ (Cost Reports + Activity) and to "
                "compute billed-MTD caps. No setup needed; it's part of "
                "the install like the VPC / RDS / ALB."
            )
        # #875: the ingress-CIDR step is a choice set (detect / custom /
        # open-all), not a bare question — route it through _ask_cidrs.
        # #877: the container-image step is likewise a 3-way (prebuilt /
        # build / custom URI) — route it through _ask_image.
        if q.key == "ingress_cidrs":
            answers = _ask_cidrs(resolver, answers)
        elif q.key == "image":
            answers = _ask_image(resolver, answers)
        else:
            answers[q.key] = resolver.ask(q)
        # #774: BYO-VPC choice belongs with the ECS path only — the
        # --local docker-compose path has no VPC. Ask it right after
        # region so the pick-list uses the chosen region.
        if not local and q.key == "region":
            answers = _ask_vpc(resolver, answers)
    if not local:
        answers = _ask_cert(resolver, answers)
        # Invocation-logging capture regions — ECS path only (it
        # provisions per-region S3 stacks + logging config; the --local
        # docker-compose path has no per-region infra to stand up).
        answers = _ask_invlogs(resolver, answers)
    answers = _ask_oidc(resolver, answers)
    return answers


def _warn_if_tg_managed(resolver: Resolver, vpc_id: str, answers: dict) -> None:
    """Warn (don't block) when a supplied/pre-set vpc_id resolves to
    tg's own VPC. Deploying into it as BYO flips create-new→BYO mode,
    which CFN can't do in place. A hard reject would be too aggressive
    for a scripted edge case, so this surfaces a loud note and carries
    the id. Best-effort: any AWS error → no note (list_vpcs returns [])."""
    if not vpc_id:
        return
    from . import runner
    region = answers.get("region", "us-east-1")
    profile = answers.get("profile")
    try:
        vpcs = runner.list_vpcs(region, profile)
    except Exception:
        return
    match = next((v for v in vpcs if v.get("id") == vpc_id), None)
    if match and match.get("tg_managed"):
        resolver.note(
            f"{vpc_id} looks like tg's own VPC (tg-managed). If this "
            "account already has a create-new tg install, deploying "
            "into it as a BYO VPC will fail the stack update (CFN can't "
            "convert create-new↔BYO in place). Use a different VPC "
            "unless you're sure."
        )


def _ask_vpc(resolver: Resolver, answers: dict) -> dict:
    """#774: create-new (default) vs bring-your-own VPC. On BYO +
    interactive, query the account's VPCs/subnets and present a
    pick-list; non-interactive (or no AWS reachable) falls back to
    validated TG_VPC_ID / TG_SUBNET_IDS. Sets vpc_id + subnet_ids
    (empty vpc_id = create-new — the default, byte-identical path).
    """
    # #962: on a detected UPGRADE, VPC mode is LOCKED to the deployed
    # stack's mode (a create-new↔BYO flip can't happen in place — the
    # #961 footgun). cmd_install already seeded vpc_mode + vpc_id/
    # subnet_ids from the deployed params, so skip the VPC/subnet
    # questions entirely and carry those forward unchanged.
    if answers.get("_is_upgrade"):
        answers.setdefault("vpc_mode", VPC_CREATE)
        answers.setdefault("vpc_id", "")
        answers.setdefault("subnet_ids", "")
        return answers
    # A pre-supplied VPC id (TG_VPC_ID via env/config, or scripted in
    # tests) means existing-VPC unambiguously — skip the mode question
    # and validate-carry the id + subnets. A non-empty id always wins
    # over any vpc_mode value.
    pre_vpc = (
        answers.get("vpc_id")
        or resolver.supplied.get("vpc_id")
        or (resolver.scripted or {}).get("vpc_id")
    )
    if pre_vpc:
        answers["vpc_mode"] = VPC_EXISTING
        answers["vpc_id"] = resolver.ask(Question(
            key="vpc_id", prompt="Existing VPC id", why="WHY: BYO VPC.",
            default=pre_vpc, validate=V.vpc_id))
        # A supplied id that resolves to tg's OWN VPC would flip a
        # create-new install to BYO in place → CFN UPDATE_FAILED. We
        # don't hard-reject (a scripted edge case may genuinely mean
        # it), but warn loudly so it isn't carried silently.
        _warn_if_tg_managed(resolver, answers["vpc_id"], answers)
        answers["subnet_ids"] = resolver.ask(Question(
            key="subnet_ids", prompt="Subnet ids", why="WHY: ≥2, ≥2 AZs.",
            default=answers.get("subnet_ids"), validate=V.subnet_ids))
        return answers

    default_mode = VPC_CREATE
    mode = resolver.ask(
        Question(
            key="vpc_mode",
            prompt="VPC for the stack — create new, or use an existing one?",
            why="WHY: tg creates a 2-AZ VPC by default. On an account at "
            "its VPC limit (or with a mandated shared VPC), reuse an "
            "existing one instead — pick ≥2 subnets across ≥2 AZs.",
            default=answers.get("vpc_mode", default_mode),
            choices=[VPC_CREATE, VPC_EXISTING],
        )
    )
    answers["vpc_mode"] = mode
    if mode == VPC_CREATE:
        answers["vpc_id"] = ""
        answers["subnet_ids"] = ""
        return answers

    region = answers.get("region", "us-east-1")
    profile = answers.get("profile")

    # mode == VPC_EXISTING with no pre-set id (a pre-supplied id is
    # handled above): live pick-list when interactive + reachable,
    # else the validated comma-separated entry question.
    from . import runner
    vpcs = runner.list_vpcs(region, profile) if resolver.interactive else []
    # Exclude tg's OWN VPC(s) from the BYO choices: selecting one flips
    # a create-new install to BYO in place, which CFN can't do (the
    # RDS/ALB are already live in those subnets) → UPDATE_FAILED. The
    # one exception: if a tg-managed VPC is the ONLY thing in the
    # account, still show it but clearly flagged, and warn + re-ask if
    # it's picked — never silently let the destructive mode-flip
    # through, but don't dead-end the wizard either.
    selectable = [v for v in vpcs if not v.get("tg_managed")]
    show = selectable or vpcs
    if vpcs:
        def _label(v):
            base = (
                f"{v['id']}  {v.get('cidr', '')}  "
                f"{v.get('name') or ('(default)' if v.get('default') else '—')}"
            )
            if v.get("tg_managed"):
                base += "  (tg-managed — do not select for BYO; " \
                        "this is tg's own VPC)"
            return base
        labels = {_label(v): v['id'] for v in show}
        managed_ids = {v['id'] for v in show if v.get("tg_managed")}
        pick = resolver.ask(
            Question(
                key="vpc_pick",
                prompt="Choose the VPC to deploy into",
                why="WHY: the stack's ALB/RDS/ECS go in this VPC's subnets.",
                default=None,
                choices=list(labels.keys()),
            )
        )
        chosen = labels.get(pick, pick)
        # Edge case: the only VPC shown was tg-managed and the user
        # picked it anyway — warn it will fail the update and re-ask.
        if chosen in managed_ids:
            resolver.note(
                f"{chosen} is tg's own VPC (tg-managed). Deploying into "
                "it as a BYO VPC flips this stack from create-new to "
                "bring-your-own mode, which CloudFormation can't do in "
                "place — the update fails and rolls back. Choose a "
                "different VPC, or create a new one."
            )
            return _ask_vpc(resolver, answers)
        answers["vpc_id"] = chosen
    else:
        answers["vpc_id"] = resolver.ask(
            Question(
                key="vpc_id",
                prompt="Existing VPC id (e.g. vpc-0abc123)",
                why="WHY: deploy into this VPC instead of creating one.",
                default=answers.get("vpc_id"),
                validate=V.vpc_id,
            )
        )
    answers["subnet_ids"] = _ask_subnets(
        resolver, answers, answers["vpc_id"], region, profile
    )
    return answers


def _ask_subnets(resolver, answers, vpc_id, region, profile) -> str:
    """#774: pick ≥2 subnets (≥2 AZs) in the chosen VPC. Live
    multiselect when interactive + reachable; else validated
    comma-separated entry (TG_SUBNET_IDS). Returns a comma-joined
    id string."""
    from . import runner
    subs = runner.list_subnets(vpc_id, region, profile) if resolver.interactive else []
    if subs and _HAVE_MULTISELECT(resolver):
        # #959: label by ROUTE egress (the attribute the deploy gates
        # on), not MapPublicIpOnLaunch — they're independent, so the
        # old 'public'/'private' label could disagree with what the
        # preflight measured and mislead the picker.
        rows = {
            f"{s['id']}  {s.get('az', '')}  {s.get('cidr', '')}  "
            f"{_EGRESS_LABEL.get(s.get('egress'), s.get('egress', '?'))}": s
            for s in subs
        }
        picked = resolver.ask_multi(
            Question(
                key="subnet_pick",
                prompt="Choose ≥2 subnets across ≥2 AZs",
                why="WHY: RDS + ALB need ≥2 AZs (#480), and all chosen "
                "subnets must share ONE egress type (all public-IGW, or "
                "all NAT, or all private-with-endpoints) — a Fargate task "
                "applies one public-IP setting to every subnet.",
                default=None,
                choices=list(rows.keys()),
            )
        )
        ids = [rows[p]["id"] for p in picked]
        # AZ-distinctness guard (the 2-AZ floor) — re-ask on failure.
        # The note must NAME how to select, because the most common
        # failure is a first-timer pressing Enter on an empty list
        # (single-select mental model) — the bare WHY re-print read as
        # an infinite loop. Branch the message: 0/<2 picked tells them
        # to use Spacebar; ≥2 picked but <2 AZs is the real AZ-floor.
        azs = {rows[p].get("az") for p in picked}
        if len(ids) < 2:
            resolver.note(
                f"You selected {len(ids)} subnet(s). Use Spacebar to "
                "check at least 2 subnets (in 2 different AZs), then "
                "press Enter to confirm."
            )
            return _ask_subnets(resolver, answers, vpc_id, region, profile)
        if len(azs) < 2:
            resolver.note(
                "Those subnets are all in one AZ. Pick ≥2 subnets "
                "across ≥2 distinct AZs, then press Enter."
            )
            return _ask_subnets(resolver, answers, vpc_id, region, profile)
        # #959: egress-homogeneity guard — sibling to the AZ-floor. Move
        # the #779 deploy-time check UP to the prompt so a mixed set is
        # rejected where it's recoverable, not after the ACM cert is
        # created. Same rule + message as _byo_egress_preflight.
        egresses = {rows[p].get("egress") for p in picked}
        err = _egress_homogeneity_error(
            egresses, vpc_id, region, profile)
        if err:
            resolver.note(err)
            return _ask_subnets(resolver, answers, vpc_id, region, profile)
        return ",".join(ids)
    # Fallback: comma-separated entry, validated (≥2, subnet-shaped).
    # #959: if the VPC's subnets are reachable, classify the typed set
    # too so scripted-but-interactive entry gets the same early signal
    # (the deploy preflight is still the backstop for non-interactive).
    while True:
        typed = resolver.ask(
            Question(
                key="subnet_ids",
                prompt="Subnet ids in this VPC, comma-separated (≥2, ≥2 AZs)",
                why="WHY: RDS + ALB need ≥2 AZs (#480), and all subnets "
                "must share ONE egress type (all public-IGW, all NAT, or "
                "all private-with-endpoints) — verified here + at deploy.",
                default=answers.get("subnet_ids"),
                validate=V.subnet_ids,
            )
        )
        if not (subs and resolver.interactive):
            return typed
        by_id = {s["id"]: s for s in subs}
        chosen = [t.strip() for t in typed.split(",") if t.strip()]
        egresses = {by_id[c].get("egress") for c in chosen if c in by_id}
        # Only judge when we could classify every typed id; an unknown
        # id falls through to the deploy preflight (don't block on a
        # typo we can't reason about).
        if len(egresses) <= 0 or any(c not in by_id for c in chosen):
            return typed
        err = _egress_homogeneity_error(
            egresses, vpc_id, region, profile)
        if not err:
            return typed
        resolver.note(err)


# #959: route-egress display labels for the subnet pick-list (the
# value the deploy preflight gates on, not MapPublicIpOnLaunch).
_EGRESS_LABEL = {
    "public": "public(IGW)",
    "nat": "nat",
    "none": "no-egress",
}


def _egress_homogeneity_error(egresses, vpc_id, region, profile):
    """#959/#779: return a re-ask message if the chosen subnets' egress
    set isn't deploy-valid, else None. Valid sets: all 'public', all
    'nat', or all 'none' WHEN the VPC has the 4 interface endpoints
    (secretsmanager + ecr.api + ecr.dkr + logs). Mirrors
    _byo_egress_preflight so the wizard verdict == the deploy verdict."""
    types = {e for e in egresses if e}
    if len(types) > 1:
        return (
            "Those subnets mix egress types "
            f"({', '.join(sorted(types))}). A Fargate task's public-IP "
            "setting is ONE value for all its subnets, so choose a "
            "consistent set: all public (IGW), or all NAT, or all "
            "private with the SM/ECR/Logs interface endpoints. Re-pick."
        )
    if types == {"none"}:
        from . import runner
        n_ep = runner.vpc_interface_endpoint_count(vpc_id, region, profile)
        if n_ep < 4:
            return (
                "Those subnets are private with no NAT route, and this "
                f"VPC lacks the required interface endpoints ({n_ep}/4: "
                "needs secretsmanager + ecr.api + ecr.dkr + logs). The "
                "task couldn't reach Secrets Manager/ECR/Logs and won't "
                "start. Pick NAT or public subnets, or add those 4 "
                "endpoints, then re-pick."
            )
    return None


def _HAVE_MULTISELECT(resolver) -> bool:
    """True when the resolver supports a multi-select prompt
    (questionary checkbox). The plain/scripted resolvers expose
    ask_multi too — see prompts.Resolver."""
    return hasattr(resolver, "ask_multi")


def _ask_oidc(resolver: Resolver, answers: dict) -> dict:
    """Login config. #926: `tg install` is **Cognito-only** — the
    installer always stands up tg-cognito-pool and derives the OIDC
    values from it. There is NO provider question and NO bring-your-own
    OIDC prompts; SAML/OIDC federation is turned on AFTER install via
    the runtime `tg_owns_directory` DB flag (+ a future IdP-config
    screen), never in the installer (#926 removed the Okta two-phase
    path).

    Login is ALWAYS on — `tg install` never offers an unauthenticated
    install. The only route to login-off is the TG_AUTH_REQUIRE_LOGIN=0
    env backfill (a dev/test escape hatch gated by the shell
    installer's prod hard-fail), never a wizard answer.
    """
    answers.setdefault("enable_login", "y")
    # Cognito is the sole install provider — pin it and clear any OIDC
    # trio so a stale env/config value can't route the (removed) Okta
    # path. The installer fills ISSUER/CLIENT_ID/secret from the pool.
    answers["auth_provider"] = AUTH_COGNITO
    answers["oidc_issuer"] = ""
    answers["oidc_client_id"] = ""
    return answers


def _cert_arn_question(answers: dict) -> Question:
    """The hand-typed ACM ARN Question — the pre-supplied / manual-escape
    / empty-list fallback path (byte-identical to the pre-#988 prompt)."""
    return Question(
        key="cert_arn",
        prompt="ACM certificate ARN",
        why="WHY: the TLS cert the ALB uses to terminate HTTPS "
        ":443. It's an AWS Certificate Manager (ACM) cert, named "
        "by its ARN (arn:aws:acm:us-east-1:<acct>:certificate/"
        "<id>). Find one in the ACM console (us-east-1) or run: "
        "aws acm list-certificates --region us-east-1 --query "
        "'CertificateSummaryList[].CertificateArn'. No cert yet? "
        "Pick the self-signed option instead (tg generates one; "
        "browser warning), or set TG_ISSUE_ACM_CERT=1 + a domain "
        "+ hosted zone to auto-issue a public one. The installer "
        "verifies the ARN exists + is ISSUED before deploying.",
        default=answers.get("cert_arn"),
        validate=V.cert_arn,
    )


def _ask_cert_arn(resolver: Resolver, answers: dict) -> str:
    """#988: resolve the existing-cert ARN. A pre-supplied value
    (env/config/scripted) or a non-interactive run flows straight
    through the hand-typed Question — byte-identical to pre-#988, and it
    NEVER calls list-certificates. Interactive + no pre-set value lists
    the ISSUED ACM certs in the target region and offers a pick-by-domain
    menu (+ a manual-entry escape); an empty/error list falls through to
    the manual Question. Mirrors _ask_image (#877)."""
    # #995: gate the skip on the EXPLICIT-supply signals ONLY
    # (resolver.supplied = env/CLI flags, resolver.scripted), NOT on
    # answers["cert_arn"]. #962 seeds answers["cert_arn"] from the
    # deployed stack's CertificateArn on an UPGRADE — that's a default
    # to pre-select, not an operator supply, and conflating the two made
    # the pick-list never appear on a re-install (it only ever fired on
    # greenfield). The seeded value is honored below as the menu default.
    supplied = (
        resolver.supplied.get("cert_arn")
        or (resolver.scripted or {}).get("cert_arn")
    )
    if supplied or not resolver.interactive:
        return resolver.ask(_cert_arn_question(answers))

    from . import runner
    # OQ2: list in the chosen region (answers["region"]), not a hardcoded
    # pin — correct if the us-east-1 pin ever loosens.
    region = answers.get("region", "us-east-1")
    certs = runner.list_acm_certs(region, answers.get("profile"))
    if not certs:
        resolver.note(
            f"No ISSUED ACM certs found in {region} (or AWS wasn't "
            "reachable) — enter an ARN manually, or restart and pick the "
            "self-signed option.")
        return resolver.ask(_cert_arn_question(answers))

    labels = {_cert_label(c.get("domain"), c.get("arn")): c["arn"]
              for c in certs if c.get("arn")}
    labels[CERT_ARN_MANUAL] = CERT_ARN_MANUAL
    # #995: pre-select the seeded/deployed cert (answers["cert_arn"], set
    # by #962 on an upgrade) as the menu default when it's in the ISSUED
    # list — so Enter keeps the live config (#962's reproduce-on-re-run
    # promise) while still showing the menu. Not in the list (rotated /
    # deleted) → first entry default; the old ARN stays reachable via the
    # manual-entry escape (re-validated at deploy, #888).
    seeded = answers.get("cert_arn")
    default_label = next(
        (lbl for lbl, arn in labels.items() if arn == seeded),
        list(labels.keys())[0])
    pick = resolver.ask(
        Question(
            key="cert_pick",
            prompt="Which ACM certificate?",
            why=f"WHY: the TLS cert the ALB uses to terminate HTTPS "
            f":443. Listing ISSUED certs in {region}; pick by domain, or "
            "enter an ARN manually.",
            default=default_label,
            choices=list(labels.keys()),
        )
    )
    chosen = labels.get(pick, pick)
    if chosen == CERT_ARN_MANUAL:
        return resolver.ask(_cert_arn_question(answers))
    return chosen


def _ask_cert(resolver: Resolver, answers: dict) -> dict:
    """The 3-way TLS choice (#484/#487). Sets cert_mode + related keys."""
    choice = resolver.ask(
        Question(
            key="cert_mode",
            prompt="TLS for the ALB endpoint — choose one",
            why="WHY: the ALB is the only endpoint. HTTPS needs a cert; "
            "HTTP is never silent (explicit opt-in only).",
            default=answers.get("cert_mode", CERT_EXISTING),
            choices=[CERT_EXISTING, CERT_SELFSIGNED, CERT_PLAINTEXT],
        )
    )
    answers["cert_mode"] = choice
    if choice == CERT_EXISTING:
        answers["cert_arn"] = _ask_cert_arn(resolver, answers)
    elif choice == CERT_PLAINTEXT:
        confirm = resolver.ask(
            Question(
                key="plaintext_confirm",
                prompt="Serve plain HTTP (no TLS)? type 'yes' to confirm",
                why="SECURITY: traffic is unencrypted. Only safe behind a "
                "tight CIDR allowlist + the login wall. The deploy fails "
                "unless you explicitly opt in.",
                default="no",
            )
        )
        if not V.is_yes(confirm):
            # Bounce back to the cert choice rather than silently proceed.
            answers.pop("cert_mode", None)
            return _ask_cert(resolver, answers)
    # CERT_SELFSIGNED needs no extra answer here — the helper is invoked
    # at deploy time (it needs the ALB DNS / runs under operator creds).
    return answers


def cert_scheme(answers: dict) -> str:
    """https when a cert/ACM-issue is configured, else http — mirrors
    the installer's endpoint-scheme logic. Used to build the OIDC
    redirect URI from the ALB DNS (#485)."""
    mode = answers.get("cert_mode")
    if mode in (CERT_EXISTING, CERT_SELFSIGNED) or answers.get("issue_acm_cert"):
        return "https"
    return "http"


def redirect_uri(answers: dict, alb_dns: str) -> str:
    """The OIDC callback URL the operator must register, derived from the
    phase-1 ALB DNS (#485). Matches the installer's API_HOST logic."""
    host = answers.get("domain_name") or alb_dns
    return f"{cert_scheme(answers)}://{host}/auth/callback"


def to_env(answers: dict, phase: int = 2) -> dict:
    """Map collected answers → the TG_* env contract for the installer.

    `phase` drives the OIDC two-phase bootstrap (#485):
      * phase 1 — deploy with the login gate OFF so the app boots
        before the OIDC redirect URI (which needs the ALB DNS) exists.
      * phase 2 — login ON + full OIDC env (redirect URI now known).
    When login is disabled outright, there's only one phase.

    Secrets are NOT included here (the OIDC client secret is threaded
    through separately and never persisted).
    """
    env: dict[str, str] = {}
    env["AWS_REGION"] = answers["region"]
    env["TG_TARGET_ACCOUNT_ID"] = answers.get("account_id", "")
    # ECS/ALB-only — absent on the --local path (reduced question
    # set, #530 phase 2). .get() keeps to_env valid for both; empty
    # values are filtered out by runner._merged_env.
    if answers.get("ingress_cidrs"):
        env["TG_ALLOWED_INGRESS_CIDRS"] = answers["ingress_cidrs"]
    env["TG_BOOTSTRAP_ADMIN_EMAIL"] = answers["bootstrap_email"]
    # #921: optional operator-provided admin password (Option B). Only
    # emit when non-empty — a blank answer means "random throwaway +
    # forgot-password" (Option A), which the installer handles by the
    # env var simply being unset. SECRET: never persisted to config
    # (config.SECRET_KEYS strips it); runner threads it to the
    # installer's env for this run only.
    if answers.get("bootstrap_password"):
        env["TG_BOOTSTRAP_ADMIN_PASSWORD"] = answers["bootstrap_password"]
    if answers.get("image") and answers["image"] != "build":
        env["TG_ECS_IMAGE_URI"] = answers["image"]

    # Bedrock invocation-logging capture regions (comma-separated). Only
    # emitted when non-empty — a blank answer means "skip logging", and
    # emitting nothing keeps the greenfield path byte-identical. The
    # installer loops these deploying the per-region capture stack +
    # seeding the admin_config catalog.
    if answers.get("invlogs_regions", "").strip():
        env["TG_INVLOGS_REGIONS"] = answers["invlogs_regions"].strip()

    # #774: BYO VPC → the installer's TG_VPC_ID / TG_SUBNET_IDS, which
    # map to the ExistingVpcId / ExistingSubnetIds CFN params. Empty
    # vpc_id = create-new (the default; nothing emitted, byte-identical
    # greenfield path).
    if answers.get("vpc_id"):
        env["TG_VPC_ID"] = answers["vpc_id"]
        if answers.get("subnet_ids"):
            env["TG_SUBNET_IDS"] = answers["subnet_ids"]

    # Cert 3-way → the installer's cert-agnostic env (#484).
    mode = answers.get("cert_mode")
    if mode == CERT_EXISTING:
        env["TG_CERT_ARN"] = answers.get("cert_arn", "")
    elif mode == CERT_PLAINTEXT:
        env["TG_ALLOW_PLAINTEXT_ALB"] = "1"
    # CERT_SELFSIGNED: TG_CERT_ARN is filled in at deploy time after the
    # helper runs (see runner.py) — not known until the ALB DNS exists.

    # OIDC two-phase login gate (#485). Login defaults ON; login-off is
    # reachable ONLY via the TG_AUTH_REQUIRE_LOGIN=0 env backfill (the
    # dev/test escape hatch — never a wizard answer), and the shell
    # installer hard-fails it on a prod environment.
    login_on = V.is_yes(answers.get("enable_login", "y"))
    if not login_on:
        env["TG_AUTH_REQUIRE_LOGIN"] = "0"
        return env
    # #796 (#782): the provider. cognito (default) lets the installer
    # stand up tg-cognito-pool; okta is bring-your-own OIDC. The shell
    # installer reads TG_AUTH_PROVIDER and gates its OIDC hard-fail on it.
    env["TG_AUTH_PROVIDER"] = (
        "okta" if answers.get("auth_provider") == AUTH_OKTA else "cognito")
    if phase == 1:
        # Phase 1: boot the app WITHOUT the login gate so the ALB DNS
        # (hence the redirect URI) becomes knowable. OIDC stays unset.
        env["TG_AUTH_REQUIRE_LOGIN"] = "0"
        return env
    # Phase 2: login on. On okta, pass the bring-your-own OIDC env (the
    # redirect URI was derived from the phase-1 ALB DNS). On cognito the
    # installer derives ISSUER/CLIENT_ID/REDIRECT_URI from the pool, so
    # leave them unset here.
    env["TG_AUTH_REQUIRE_LOGIN"] = "1"
    if env["TG_AUTH_PROVIDER"] == "okta":
        env["TG_OIDC_ISSUER"] = answers.get("oidc_issuer", "")
        env["TG_OIDC_CLIENT_ID"] = answers.get("oidc_client_id", "")
        env["TG_OIDC_REDIRECT_URI"] = answers.get("oidc_redirect_uri", "")
    return env
