"""`tg` CLI entrypoint — install / status / destroy (#487).

  tg install  [--non-interactive] [--dry-run] [--full-reset]
  tg status
  tg destroy  [--non-interactive] [--dry-run] [--full]

install runs the 7-question wizard (resuming from ~/.tg/config.json),
shows a confirm screen, then execs scripts/tg-ecs-install.sh. --dry-run
stops at the confirm screen + the TG_* env it would set, mutating
nothing. The OIDC client secret is prompted fresh and never persisted.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, config, runner, wizard
from .prompts import PromptAbort, Resolver
from .wizard import CERT_SELFSIGNED, run_questions, to_env

# Reserved exit code the installer (tg-ecs-install.sh `fail()`) uses for
# a deliberate abort or any PRE-DEPLOY hard-fail — the account/region
# confirm gate, a validation error, a core-stack rollback. The wizard
# treats it as FATAL regardless of stack health, so a confirm-gate abort
# is never mistaken for the cosmetic post-"Done" summary glitch and
# silently continued to CUR. MUST match TG_ABORT_EXIT in the installer.
INSTALLER_ABORT_EXIT = 3


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tg", description="Token Governance one-click installer")
    p.add_argument("--version", action="version", version=f"tg {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("install", help="run the install wizard + deploy")
    pi.add_argument("--non-interactive", action="store_true",
                    help="no prompts; pull every answer from env/config (CI)")
    pi.add_argument("--dry-run", action="store_true",
                    help="plan only: show the confirm screen + env, never mutate")
    pi.add_argument("--full-reset", action="store_true",
                    help="ignore any saved ~/.tg/config.json and start fresh")
    # #530 phase 2: delegating flags. --local picks the
    # docker-compose dev path (coexists with the documented
    # tg-local-install.sh — open-Q4 = coexist); the --with-*
    # flags fold optional components behind the one front door.
    pi.add_argument("--local", action="store_true",
                    help="deploy the local docker-compose stack instead "
                         "of ECS (delegates to tg-local-install.sh)")
    # #1075: CUR (tg-cur-athena, the SOLE spend + discovery source, #720)
    # is a REQUIRED install step — same tier as the ALB/RDS/ECS the
    # installer always creates. There is NO opt-out: --no-cur is removed
    # and TG_SKIP_CUR is no longer honored (a cloud install fails fast if
    # it's set, so stale automation surfaces loudly). --with-cur stays as
    # a deprecated no-op alias only so existing scripts/CI don't error.
    pi.add_argument("--with-cur", action="store_true",
                    help="(deprecated; CUR is always deployed) no-op")
    # #576: --with-admin-binary removed — the tg-admin desktop
    # client is deleted (web login is the admin entry).
    pi.add_argument("--verify", action="store_true",
                    help="run the CUR-wiring verifier after install "
                         "(auto-runs after the default CUR deploy)")
    # Concise success banner by default; -v adds the
    # Advanced/troubleshooting block (health-check / API-docs / ECS
    # console / live-logs / tear-down). Conventional concise-default,
    # -v-for-detail shape (AWS CLI / SAM / CDK).
    pi.add_argument("--verbose", "-v", action="store_true",
                    help="show the Advanced/troubleshooting block "
                         "(console links + live-logs + tear-down) in the "
                         "success banner")

    sub.add_parser("status", help="report stack status / URL / admin")

    pd = sub.add_parser("destroy", help="tear down via the clean-slate verifier")
    pd.add_argument("--non-interactive", action="store_true")
    pd.add_argument("--dry-run", action="store_true",
                    help="show what destroy would target, then stop")
    pd.add_argument("--full", action="store_true",
                    help="also remove the shared bedrock-layer stacks")
    pd.add_argument("--local", action="store_true",
                    help="tear down the local docker-compose stack "
                         "(delegates to tg-local-destroy.sh)")
    return p


def _seed_answers(non_interactive: bool, full_reset: bool) -> dict:
    """Build the starting answer set: saved config + env overrides.

    #874: config is keyed on the target account so concurrent installs
    against different accounts can't cross-resume. The account is
    resolved (env → STS suggestion → wizard) BEFORE the keyed config is
    loaded, so seeding here only reads the NEUTRAL legacy config.json
    as a base; cmd_install re-loads the account-keyed config (and
    migrates a matching legacy one) once the account is known. With
    --full-reset, skip the legacy base entirely.
    """
    answers: dict = {} if full_reset else dict(config.load())
    # env overrides (so CI / advanced operators can pre-set anything)
    env_map = {
        "region": os.environ.get("AWS_REGION"),
        "account_id": os.environ.get("TG_TARGET_ACCOUNT_ID"),
        "ingress_cidrs": os.environ.get("TG_ALLOWED_INGRESS_CIDRS"),
        "bootstrap_email": os.environ.get("TG_BOOTSTRAP_ADMIN_EMAIL"),
        "cert_arn": os.environ.get("TG_CERT_ARN"),
        # #926: TG_OIDC_* are no longer install inputs — `tg install`
        # is Cognito-only. SAML/OIDC is turned on post-install via the
        # tg_owns_directory DB flag, so the installer never seeds an
        # OIDC issuer/client-id.
        # #774: BYO VPC — pre-seed so --non-interactive installs into
        # an existing VPC without prompting; also used as the wizard
        # default when set.
        "vpc_id": os.environ.get("TG_VPC_ID"),
        "subnet_ids": os.environ.get("TG_SUBNET_IDS"),
        "profile": os.environ.get("AWS_PROFILE"),
    }
    # Login gate: the wizard no longer ASKS whether to gate behind
    # login — login is always on. This TG_AUTH_REQUIRE_LOGIN=0 env
    # backfill is now the ONLY route to a login-off install, and it is
    # deliberately kept as a documented dev/test escape hatch (not a
    # wizard path). The load-bearing guarantee is the shell installer's
    # prod hard-fail: TG_AUTH_REQUIRE_LOGIN=0 + Environment=prod aborts
    # the deploy, so this backfill can only produce a login-off install
    # on a dev/stage/test environment.
    rl = os.environ.get("TG_AUTH_REQUIRE_LOGIN")
    if rl is not None:
        answers["enable_login"] = "n" if rl == "0" else "y"
    # #926: `tg install` is Cognito-only — no provider seeding. The
    # Okta/bring-your-own-OIDC install path (and its two-phase
    # redirect-URI bootstrap) is removed; SAML is turned on AFTER
    # install via the tg_owns_directory DB flag. Pin Cognito so the
    # single-phase path is taken regardless of any stale env.
    answers["auth_provider"] = wizard.AUTH_COGNITO
    for k, v in env_map.items():
        if v:
            answers[k] = v
    return answers


def _reconcile_account_config(answers: dict, acct: str) -> None:
    """#874: once the target account is known, reconcile the persisted
    config so a multi-install box can't cross-contaminate:

      * If the neutral legacy config.json was seeded into `answers`
        (in _seed_answers) but it belongs to a DIFFERENT account, warn
        loudly and drop those resumed answers — they were another
        install's, and silently resuming them is the exact bug (#874).
      * Adopt a matching/account-less legacy config.json under the
        account-keyed name, then merge the account-keyed config so a
        prior interrupted run for THIS account resumes.

    answers is mutated in place. Env-supplied values already in answers
    win over persisted ones (they're re-applied by the caller's seeding
    order; here we only fold in account-keyed resume data that isn't
    already set by env)."""
    seeded_acct = answers.get("account_id")
    # The neutral legacy file's account, if any (what _seed_answers read).
    legacy = config.load()  # neutral config.json
    legacy_acct = legacy.get("account_id")
    if legacy_acct and legacy_acct != acct:
        print(
            f"tg: WARNING — a saved config for account {legacy_acct} "
            f"was found, but you're installing into {acct}. Ignoring "
            "the saved answers for the other account (multi-install "
            "safety, #874). Your answers are saved per-account at "
            f"{config.config_path_for(acct)}.",
            file=sys.stderr,
        )
        # Drop the cross-account resumed values that _seed_answers
        # folded in from the neutral file (keep only env/this-run keys).
        for k in list(answers):
            if k in legacy and answers.get(k) == legacy.get(k) \
                    and k != "account_id":
                del answers[k]
    # Adopt a matching/account-less legacy config + load the keyed one.
    keyed = config.migrate_legacy(acct)
    # Fold keyed-resume values UNDER the current answers (env + this-run
    # prompts win; resume fills gaps only).
    for k, v in keyed.items():
        answers.setdefault(k, v)
    _ = seeded_acct  # (kept for readability; account_id already set)


def _enforce_account_match(resolver, answers, caller_acct) -> None:
    """#1027: surface a caller-account != target-account mismatch at the
    account question — BEFORE collecting the rest of the wizard and
    launching the installer.

    Today the mismatch first surfaces only at the installer's preflight
    (tg-ecs-install.sh, #1018), after the operator has answered every
    question for nothing. The caller account is the account our
    credentials actually deploy into; when it differs from the target the
    operator entered, every downstream answer is wasted. Surface it now,
    in plain language naming both accounts + the AWS_PROFILE fix (mirrors
    the installer message). The installer preflight stays the
    authoritative, non-bypassable backstop — this is an earlier,
    friendlier surface, not a replacement.

      * caller unresolvable (no creds / STS fails) → caller_acct is None
        → skip (can't compare); the preflight still backstops.
      * interactive → warn via note() + re-ask the account question;
        re-entering the SAME value confirms a deliberate cross-account
        target and proceeds. Kills the unset/wrong-profile foot-gun
        without foreclosing a genuine cross-account deploy.
      * --non-interactive → no reprompt possible: hard-fail fast
        (PromptAbort, caught by the caller) before answer-mapping /
        installer launch.

    (--dry-run callers skip this entirely — it's a creds-agnostic
    preview that mutates nothing.)
    """
    from .prompts import Question
    from . import validate as V
    while (caller_acct and answers.get("account_id")
           and caller_acct != answers["account_id"]):
        target = answers["account_id"]
        if not resolver.interactive:
            raise PromptAbort(
                f"caller account {caller_acct} != target {target}; "
                "set AWS_PROFILE for the target account and re-run")
        resolver.note(
            f"Your credentials resolve to account {caller_acct}, but "
            f"you entered target account {target}. These must match. "
            "Export the deploy profile for the target account and "
            "re-run, e.g. `export AWS_PROFILE=tg-install-<account>` — "
            "or, if you meant to deploy cross-account, re-enter the "
            "same account to confirm.")
        answers["account_id"] = resolver.ask(Question(
            key="account_id", prompt="Target AWS account id (12 digits)",
            why="WHY: the account this stack deploys into; must match "
            f"your caller identity ({caller_acct}). Re-enter the same "
            "value to confirm a deliberate cross-account target.",
            default=target, validate=V.account_id))
        # Same value back = explicit cross-account confirmation → stop
        # nagging and proceed (the installer preflight still has the final
        # say). A supplied/scripted answer can't change, so this also
        # guarantees the loop terminates.
        if answers["account_id"] == target:
            break


def cmd_install(args) -> int:
    # #1115: gate on the minimum supported Python BEFORE anything else —
    # the bash installers shell out to `python3` for JSON parsing, so a
    # too-old interpreter would otherwise crash mid-install with a cryptic
    # SyntaxError/ImportError in a subprocess. Checks the PATH `python3`
    # the scripts actually invoke. Applies to --dry-run too (the wizard
    # itself runs on this interpreter). Pure introspection — no creds.
    _py_ok, _py_ver = runner.check_python()
    if not _py_ok:
        print("\n" + runner.python_upgrade_message(_py_ver), file=sys.stderr)
        return 2
    answers = _seed_answers(args.non_interactive, args.full_reset)
    # #1130: show WHO you are + WHICH account you're deploying into at the
    # very TOP — before the build-version line and before the wizard's
    # "Target AWS account id" question — so the owner confirms identity +
    # account up front, not after answering the whole wizard. The profile
    # is already seeded from AWS_PROFILE in _seed_answers (it is NOT a
    # wizard question today), so this needs no later answer and can run
    # here unconditionally. CAVEAT: if profile ever BECOMES a wizard
    # question, this report must move to print AFTER that answer — keep
    # it env-seeded or relocate. --dry-run is creds-agnostic (mutates
    # nothing), so skip the live probe there (matches the #1093 skip).
    #
    # #1093: when AWS_PROFILE is SET but the profile name doesn't resolve
    # (a typo / nonexistent profile), abort up front NAMING the bad
    # profile — distinct from #1087's expired-session case (that profile
    # DOES exist). Kept adjacent so an unresolved-OR-expired profile fails
    # before any question, as one coherent up-front block.
    if not getattr(args, "dry_run", False):
        _bad_profile = answers.get("profile") or os.environ.get("AWS_PROFILE")
        if _bad_profile and runner.profile_not_found(_bad_profile):
            print(
                f'\ntg: AWS profile "{_bad_profile}" could not be found — '
                "check the spelling or your ~/.aws/config. Set a valid "
                "AWS_PROFILE and re-run `tg install`.",
                file=sys.stderr)
            return 2
        # #1087 (relocated by #1130): resolve + validate the AWS
        # credential source and print the identity report FIRST. The
        # single read-only get-caller-identity is the universal liveness
        # probe for every credential type; ok==False aborts up front with
        # remediation — before any wizard question / before run_install.
        pf = runner.preflight_caller({"AWS_PROFILE": answers.get("profile")})
        print(f"Using AWS credentials: {pf['source']}.")
        if pf["ok"]:
            print(f"Logged in as: {pf['arn']} (account {pf['account']}).")
        else:
            print("\ntg: could not verify AWS credentials "
                  "(aws sts get-caller-identity failed).", file=sys.stderr)
            if pf["is_sso"]:
                prof = pf["profile"]
                login = "aws sso login" + (
                    f" --profile {prof}" if prof else "")
                print(
                    f"    Your AWS SSO session is not active. Run: {login}\n"
                    "    then re-run `tg install`.", file=sys.stderr)
            else:
                print("    Credentials are invalid or expired. Fix them "
                      "(or set AWS_PROFILE) and re-run `tg install`.",
                      file=sys.stderr)
            return 2
    # #1000: print the BUILD version (git-describe scheme, the
    # meaningful "matches the latest build" signal — not the static
    # __version__), for both interactive + --non-interactive. Derived
    # ONCE here and threaded to the installer via TG_VERSION so the
    # banner and the TgVersion deploy stamp can't drift. Printed AFTER
    # the identity report (#1130) so account/arn lead.
    build_ver = runner.build_version()
    # #1104: show the bare release (v1.1.0) to the user; build_ver
    # (full v1.1.0-g<sha>[-dirty]) is still threaded to TG_VERSION below
    # so the deploy stamp + /api/version keep the full provenance.
    print(f"  tg build version: {runner.display_version(build_ver)}")
    # #881: always disclose where state lives + how to reset, on EVERY
    # run (today the path is only revealed on the Ctrl-C message). One
    # terse line; --full-reset starts clean.
    print(
        f"  tg saves your answers to {config.CONFIG_PATH} (no secrets) "
        "so an\n  interrupted install can resume. Start over with "
        "--full-reset."
    )
    # #881: "resuming" = a saved config (neutral legacy now, or the
    # account-keyed file loaded once the account is known) actually had
    # answers. Capture the neutral-legacy signal here, BEFORE the
    # account-keyed reconcile may fold more in; --full-reset is never a
    # resume (it ignores saved state by contract).
    resumed = bool(not args.full_reset and config.load())
    # account id is required and not one of the 7 prompts — derive from
    # env, else ask once up front (kept out of the numbered flow).
    # #999: resolver.supplied must be the OPERATOR-supply set (env / CLI
    # flags / config / scripted) ONLY — captured INDEPENDENTLY of the
    # mutable `answers` working dict. Passing `answers` itself aliased
    # the two: the #962 upgrade-seed writes the deployed CertificateArn
    # into answers["cert_arn"], which (under the alias) ALSO appeared in
    # resolver.supplied — so _ask_cert_arn's "skip the menu when supplied"
    # guard tripped on a mere deployed DEFAULT, defeating #995. A shallow
    # copy here freezes the true supply set at construction (config ∪
    # env, before the #962 seed mutates answers), so answers["cert_arn"]
    # (seeded default → menu pre-select) and resolver.supplied["cert_arn"]
    # (genuine supply → skip) are finally distinguishable.
    resolver = Resolver(interactive=not args.non_interactive,
                        supplied=dict(answers))
    try:
        from .prompts import Question
        from . import validate as V
        # #1027: resolve the caller account ONCE, up front, and reuse it
        # for (a) the account-question default, (b) the early caller!=target
        # mismatch surface just below, and (c) the upgrade-seeding gate
        # further down. This collapses the two old caller_account() calls
        # (the question default + the upgrade gate) into a single STS
        # round-trip.
        caller_acct = runner.caller_account(answers.get("profile"))
        if not answers.get("account_id"):
            # #874: suggest the account from STS (the caller's account)
            # so the user accepts/overrides instead of hitting a late
            # pre-flight "caller != target" hard-fail. A default only —
            # a user may legitimately target another account (e.g.
            # cross-account role assumption). Skip the probe entirely on
            # --full-reset? No — the suggestion is still useful; it's
            # just a default, never auto-applied.
            answers["account_id"] = resolver.ask(Question(
                key="account_id", prompt="Target AWS account id (12 digits)",
                why="WHY: the account this stack deploys into; must match "
                "your caller identity." + (
                    f" Detected caller account {caller_acct} — press Enter "
                    "to accept, or type another." if caller_acct else ""),
                default=answers.get("account_id") or caller_acct,
                validate=V.account_id))
        # #1027: fail fast on a caller != target account mismatch right
        # here, at the account question — before the rest of the wizard +
        # the installer launch. --dry-run is exempt (creds-agnostic
        # preview, mutates nothing). See _enforce_account_match.
        if not getattr(args, "dry_run", False):
            _enforce_account_match(resolver, answers, caller_acct)
        # #874: now that the target account is known, re-load the
        # ACCOUNT-KEYED config (migrating a matching legacy config.json),
        # and warn loudly if a stale config for a DIFFERENT account was
        # about to be resumed. Skip on --full-reset (start clean).
        acct = answers.get("account_id")
        if acct and not args.full_reset:
            # The account-keyed file is ALSO a resume source (a prior
            # interrupted run for THIS account), even when the neutral
            # legacy was empty — fold it into the resume signal.
            if config.load(acct):
                resumed = True
            _reconcile_account_config(answers, acct)
        elif acct and args.full_reset:
            # --full-reset is the clean-slate escape hatch: in addition to
            # ignoring saved answers (handled in _seed_answers), WIPE the
            # account-keyed file now so the reset actually resets on disk.
            # Done at START (account known), unconditional of the run's
            # outcome — a clean slate shouldn't depend on a successful
            # deploy. (The success-time clear was removed so a
            # normal re-install keeps its prefs; full-reset must still
            # wipe, so it clears here.)
            config.clear(acct)
        # #962: upgrade-awareness. If a tg-container-stack is already
        # deployed in this account, seed the wizard defaults from its
        # DEPLOYED CFN parameters so Enter-through reproduces the live
        # config — an in-place upgrade, not a generic-default re-install
        # that flips create-new↔BYO into a rollback (the 06-12 failure /
        # the #961 footgun, closed structurally here). The --local path
        # has no CFN stack, so skip it there. Degrades to today's
        # new-install flow on any describe error (deployed is None).
        is_upgrade = False
        # Gate on caller==target: describe-stacks reads the CALLER's
        # account, and you can only upgrade a stack in the account you
        # actually deploy to. When the operator targets a DIFFERENT
        # account (cross-account), the caller's stack is irrelevant —
        # skip detection so we never seed defaults from the wrong
        # account's stack. (A mismatch is also caught earlier — at the
        # account question, #1027 — so by here caller==target unless the
        # operator confirmed a deliberate cross-account target, in which
        # case skipping upgrade detection is exactly right.) Reuses the
        # single #1027 caller_acct resolution above — no second STS call.
        if (acct and caller_acct == acct
                and not getattr(args, "local", False)):
            deployed = runner.deployed_stack_defaults(
                answers.get("region", "us-east-1"), answers.get("profile"))
            if deployed:
                if not deployed["updatable"]:
                    print(
                        f"tg: {runner.CONTAINER_STACK} is in state "
                        f"{deployed['status']} — not an updatable state. "
                        "Wait for any in-progress operation to finish (or "
                        "resolve a failed rollback in the CloudFormation "
                        "console), then re-run `tg install`.",
                        file=sys.stderr)
                    return 1
                is_upgrade = True
                # Precedence: env/--non-interactive supplied > deployed
                # param > config file > generic default. `answers`
                # already holds (config ∪ env) with env winning; deployed
                # must beat config but yield to an env-supplied value.
                supplied = resolver.supplied or {}
                for k, v in deployed["answers"].items():
                    if k not in supplied:
                        answers[k] = v
                # vpc_mode is DERIVED on upgrade, never asked: a stack's
                # create-new↔BYO mode can't change in place (#961). On a
                # create-new stack, force create-new + clear any VPC/
                # subnet so _ask_vpc takes the no-question greenfield path.
                if deployed["vpc_mode_create_new"]:
                    answers["vpc_mode"] = wizard.VPC_CREATE
                    answers["vpc_id"] = ""
                    answers["subnet_ids"] = ""
                else:
                    answers["vpc_mode"] = wizard.VPC_EXISTING
                answers["_is_upgrade"] = True
                answers["_image_from"] = deployed["image_from"]
                print(
                    f"\n  Detected a deployed {runner.CONTAINER_STACK} "
                    f"({deployed['status']}) — this run will UPGRADE it. "
                    "Defaults are pre-filled from the live stack; press "
                    "Enter to keep each, or type a new value.")
        # #881: on a resume, replay the answers collected so far before
        # the next prompt, so the operator sees what was retained
        # instead of being dropped mid-wizard with no orientation.
        # Display-only: already-answered questions are still skipped,
        # never re-asked. Secrets are filtered defensively.
        if resumed:
            summary = runner.resume_summary(answers, config.SECRET_KEYS)
            if summary:
                print(summary)
        answers = run_questions(resolver, answers,
                                local=getattr(args, "local", False))
        config.save(answers, acct)  # persist for resume (secrets scrubbed)
    except PromptAbort as e:
        # Save progress so a re-run resumes, then exit 130 (Ctrl-C).
        config.save(answers, answers.get("account_id"))
        print(f"\ntg: {e} — progress saved to "
              f"{config.config_path_for(answers.get('account_id'))}; "
              "re-run `tg install` to resume.", file=sys.stderr)
        return 130

    login_on = wizard.V.is_yes(answers.get("enable_login", "y"))
    # #926: no `resuming` gate — install is Cognito-only / single-phase
    # (the Okta two-phase resume that read phase=awaiting-oidc-
    # registration is removed).

    if args.dry_run:
        return _dry_run(answers, login_on, args)

    # Build-from-source needs a Docker that can actually BUILD.
    # `docker info` (the installer's old check) passes even when Docker
    # Desktop's build is org-sign-in/policy blocked, so the install used
    # to die mid-build after the stack was partly up. Probe the BUILD
    # path here — before any deploy/CFN work — and stop up front with the
    # specific cause + the prebuilt-image escape. Only when building (the
    # prebuilt path needs no Docker). The bash installer keeps a backstop
    # build-smoke for a direct-script run.
    if answers.get("image") == "build":
        _dok, _dmsg = runner.docker_build_preflight()
        if not _dok:
            print("\ntg: " + _dmsg, file=sys.stderr)
            return 2

    # (Identity report + creds liveness probe ran at the TOP — #1130 /
    # #1087 — so it's NOT repeated here. A bad/expired-creds abort has
    # already fired before any wizard question.)

    # ── #530 phase 2: --local picks the docker-compose dev path ──
    # (coexists with the ECS path — open-Q4 = coexist). It skips
    # the ECS-only cert 3-way + OIDC two-phase entirely; the
    # local installer handles its own compose env.
    if getattr(args, "local", False):
        env = to_env(answers, phase=2)
        # #1000: thread the CLI-derived build version down so the local
        # installer stamps the same value the start banner showed.
        env["TG_VERSION"] = build_ver
        print(runner.render_confirm(answers, env, local=True))
        print("\n[--local] deploying the docker-compose stack…")
        rc = runner.run_local_install(env)
        if rc == 0:
            rc = _run_addons(args, env)
        # Keep config-<account>.json on success so a re-install pre-fills
        # the saved answers (tg install is idempotent + re-run for
        # upgrades/image bumps). --full-reset wipes it at START (above),
        # so no success-time clear is needed. Only stable prefs are
        # persisted — secrets are never written, transient
        # _is_upgrade/_image_from are scrubbed + re-derived each run.
        return rc

    # #926: `tg install` is Cognito-only → ALWAYS single-phase. The
    # Okta two-phase bootstrap (#485/#860 — boot login-off, register the
    # ALB-derived redirect URI in an external IdP, re-apply login-on) is
    # removed: Cognito self-provisions the pool after the ALB exists
    # (step 7a) and derives ISSUER / CLIENT_ID / REDIRECT_URI / secret
    # from the pool's own outputs, so there's no redirect-URI pause.
    # SAML/OIDC federation is turned on AFTER install via the
    # tg_owns_directory DB flag, not the installer.
    #
    # Single-phase deploy: Cognito with login ON (to_env(phase=2) sets
    # TG_AUTH_REQUIRE_LOGIN=1 + TG_AUTH_PROVIDER=cognito and leaves OIDC
    # vars unset for the installer to derive), or an explicit login-off
    # dev/test install (the TG_AUTH_REQUIRE_LOGIN=0 escape hatch).
    env = to_env(answers, phase=2)
    # #1000: thread the CLI-derived build version so the installer's
    # TgVersion stamp == the start banner (single source, no drift).
    env["TG_VERSION"] = build_ver
    # Propagate --verbose so the bash installer's own banner block (when
    # it prints one) matches the wizard's gating. The wizard owns the
    # Done banner under TG_SUMMARY_OUT, so this mainly aligns a fallback
    # path; harmless when unset.
    if getattr(args, "verbose", False):
        env["TG_VERBOSE"] = "1"
    _maybe_selfsigned(answers, env)
    # #1119: hoist the CUR reuse-vs-create decision into the Q&A here,
    # BEFORE any step runs, and thread it down as TG_CUR_DECISION so
    # tg-cur-deploy.sh runs non-interactively (no mid-install stdin
    # block / invisible-prompt hang). Only ask when a candidate exists;
    # cloud installs only (--local has no cloud CUR).
    if not getattr(args, "local", False):
        _decision = _ask_cur_decision(answers)
        if _decision:
            env["TG_CUR_DECISION"] = _decision
        # #1123: lockstep image<->CFN-template gate. When deploying a
        # PREBUILT image (TG_ECS_IMAGE_URI; a build-from-source image is
        # fresh from THIS checkout, so always version-matched), refuse to
        # deploy if it's OLDER than the CUR template's TgMinImageVersion
        # requires — caught here, before the stack create, not at the
        # customer's first query (the {{DATE_FILTER}} deploy-skew). A
        # label that can't be read → warn, never hard-refuse.
        _img = env.get("TG_ECS_IMAGE_URI")
        if _img:
            _status, _msg = runner.check_image_template_compat(
                _img, runner.CUR_DEPLOY_TEMPLATE)
            if _status == "skew":
                print(f"\ntg: {_msg}", file=sys.stderr)
                return 2
            if _status == "warn":
                print(f"\ntg: warning — {_msg}", file=sys.stderr)
    rc, _summary = runner.run_install(env)
    # #1067: the installer's post-"Done" summary is decorative — a
    # cosmetic failure there (e.g. a stray heredoc backslash, the demo2
    # 96c4e4c EOF abort) must NOT be reported as a stack failure nor skip
    # CUR. assert_stack_succeeded in tg-ecs-install.sh makes the CFN
    # status the source of truth for core-install health, so when the
    # wrapper exits non-zero but tg-container-stack is actually in a
    # healthy terminal state, treat the install as succeeded: CUR still
    # runs, and we don't mislabel a healthy core as failed.
    #
    # BUT this must NOT swallow a deliberate abort / pre-deploy hard-fail
    # (the account/region confirm gate, a validation error, a core-stack
    # rollback): the installer signals those with INSTALLER_ABORT_EXIT,
    # which is FATAL regardless of stack health. On a RE-RUN the container
    # stack already exists + is healthy from a prior install, so health
    # alone can't tell "operator said no" from "only the banner glitched"
    # — the exit code is the discriminator. Ignore-and-continue ONLY for a
    # non-abort, non-zero exit (the cosmetic summary case) WITH a healthy
    # stack; every other non-zero (incl. the abort) falls through to the
    # fatal branch.
    if _ignore_cosmetic_nonzero(rc) and _core_stack_healthy(answers):
        print("\ntg: core install is healthy (tg-container-stack "
              "succeeded); ignoring a non-zero exit from the install "
              "summary and continuing.", file=sys.stderr)
        rc = 0
    if rc == 0:
        rc = _run_addons(args, env)
        # #1119: the WIZARD owns the single "Done" banner — printed LAST,
        # after BOTH ECS and CUR succeed (never from inside the ECS
        # sub-step, which would land it before "deploying CUR…"). The ECS
        # step emitted its summary values to a temp file (TG_SUMMARY_OUT,
        # captured in _summary); we render the banner here with a CUR
        # status line folded in. #1075: CUR is always deployed on a cloud
        # install (rc==0 means it succeeded); --local has no cloud CUR.
        if rc == 0:
            cur_line = None
            if not getattr(args, "local", False):
                cur_line = (
                    "Cost reporting: configured — Cost Reports populate "
                    "in ~24h (AWS CUR 2.0\n"
                    "                delivers the first file 24-48h after "
                    "setup). The app is\n"
                    "                fully usable now.")
            if _summary:
                print(runner.render_done_banner(
                    _summary, cur_line,
                    verbose=getattr(args, "verbose", False)))
            elif cur_line:
                # Handoff file missing (older script / pre-summary exit):
                # fall back to the CUR reassurance line so the user still
                # sees the cost-reporting status.
                print("\n" + cur_line)
        # #1000: confirm the DEPLOYED /api/version matches the build we
        # just stamped — closes the operator's "does it match the latest
        # build" question. Best-effort: skip the line if the app isn't
        # reachable yet (don't fail the install on a version probe).
        if rc == 0:
            _print_version_match(answers, build_ver)
    elif rc == INSTALLER_ABORT_EXIT:
        # The operator aborted at a confirm gate, or a pre-deploy
        # hard-fail / core-stack rollback tripped — the installer
        # (`fail()`) already printed the SPECIFIC reason (e.g. "✗ Aborted
        # — no resources created." or the rollback status). FATAL: CUR is
        # NOT attempted and we do not report success — even on a re-run
        # where tg-container-stack already exists from a prior install
        # (the bug this guards: health alone was treating "operator said
        # no" as a cosmetic glitch and continuing to CUR). No extra line —
        # the installer's own message is the clear one; adding "core
        # install failed" here would mislabel a deliberate abort.
        pass
    else:
        # A non-abort, non-cosmetic failure with an unhealthy core stack
        # (the installer asserts a non-rollback terminal status; a
        # core-stack failure that didn't go through `fail()` lands here).
        # FATAL: CUR is NOT attempted, no success banner. Distinct from
        # the best-effort CUR-only warning (there the core app is up;
        # here it isn't).
        print("\ntg: the core install (tg-container-stack) failed — CUR "
              "2.0 was NOT attempted. Fix the stack error above, then "
              "re-run `tg install` (it's idempotent once the cause is "
              "cleared).", file=sys.stderr)
    # Keep config-<account>.json on success so a re-install pre-fills the
    # saved answers (see the --local path above). --full-reset wipes it at
    # START; secrets are never persisted; transient state is scrubbed +
    # re-derived — so there's no stale-defaults trap.
    return rc


def _print_version_match(answers: dict, build_ver: str) -> None:
    """#1000: read the deployed app's /api/version and print whether it
    matches the build just deployed — the explicit answer to "does it
    match the latest build". Best-effort: any failure to resolve the URL
    or reach the app → print nothing (never fail the install on a probe;
    the app may still be warming up). The public origin mirrors the
    installer: a custom domain if set, else the ALB DNS stack output;
    https unless the explicit plaintext opt-in."""
    region = answers.get("region", "us-east-1")
    profile = answers.get("profile")
    host = answers.get("domain_name") or runner.stack_output(
        runner.CONTAINER_STACK, "AlbDnsName", region, profile)
    if not host:
        return
    scheme = "http" if answers.get("cert_mode") == wizard.CERT_PLAINTEXT \
        else "https"
    deployed = runner.deployed_version(f"{scheme}://{host}")
    if not deployed:
        return                       # app not reachable yet — stay quiet
    if deployed == build_ver:
        print(f"\n✓ Running version matches this build ({build_ver}).")
    else:
        print(
            f"\n! Running version is {deployed}, but this build is "
            f"{build_ver}. If you expected them to match, the deploy may "
            "have reused a cached image — re-run `tg install` or check "
            "the ECS service's task definition image.")


def _ignore_cosmetic_nonzero(rc: int) -> bool:
    """True iff a non-zero installer exit `rc` is a candidate for the
    "ignore & continue to CUR" path — i.e. it MIGHT be the cosmetic
    post-"Done" summary glitch (a healthy core, only the trailing banner
    errored). That is every non-zero EXCEPT INSTALLER_ABORT_EXIT, which
    the installer reserves for a deliberate abort / pre-deploy hard-fail
    / core-stack rollback and is ALWAYS fatal. The caller still requires
    a healthy stack on top of this before actually continuing — this
    predicate only screens out the never-ignore abort code (the fix: on a
    re-run, a healthy stack alone must not authorize ignoring an abort)."""
    return rc != 0 and rc != INSTALLER_ABORT_EXIT


def _core_stack_healthy(answers: dict) -> bool:
    """#1067: True iff tg-container-stack is in a healthy terminal CFN
    status (CREATE/UPDATE_COMPLETE). Read-only; the source of truth for
    core-install health, independent of the wrapper script's exit code
    — so a cosmetic summary-print failure can't make a green install
    look failed or skip CUR. Any probe error → False (fall through to
    the existing failure path; never claim health we can't confirm)."""
    region = answers.get("region", "us-east-1")
    profile = answers.get("profile")
    info = runner.describe_stack(runner.CONTAINER_STACK, region, profile)
    if not info:
        return False
    return info.get("Status") in ("CREATE_COMPLETE", "UPDATE_COMPLETE")


def _check_no_stale_skip_cur() -> None:
    """#1075: CUR is required — TG_SKIP_CUR is no longer honored. If a
    stale automation env still sets it, fail FAST with a clear message
    rather than silently giving the operator CUR they tried to opt out
    of (a silent behavior change is the worse surprise). Cloud install
    only — the caller gates this on `not args.local`."""
    if os.environ.get("TG_SKIP_CUR", "") not in ("", "0"):
        raise SystemExit(
            "tg: TG_SKIP_CUR is no longer supported — CUR 2.0 "
            "(tg-cur-athena) is a required part of the install (it's "
            "the sole spend + discovery source). Unset TG_SKIP_CUR and "
            "re-run `tg install`.")


def _ask_cur_decision(answers: dict) -> str | None:
    """#1119: hoist the CUR reuse-vs-create decision into the wizard Q&A
    so tg-cur-deploy.sh runs non-interactively (no mid-install invisible
    prompt). Returns "reuse" | "create" to thread as TG_CUR_DECISION, or
    None to leave the script's own logic (interactive/default) in charge.

    - A pre-set TG_CUR_DECISION (CI/automation) wins, untouched.
    - Else probe read-only for a reusable export. None → return None (no
      question; the script create-defaults safely).
    - A candidate exists + interactive TTY → ask reuse/create, default
      reuse (the operator already has a healthy export). Non-interactive
      with a candidate → return None and let the script's safe
      create-default apply (never auto-attach unattended)."""
    preset = os.environ.get("TG_CUR_DECISION", "").strip().lower()
    if preset in ("reuse", "create"):
        return preset
    region = answers.get("region", "us-east-1")
    profile = answers.get("profile")
    candidate = runner.cur_reuse_candidate(region, profile)
    if not candidate:
        return None
    if not sys.stdin.isatty():
        # A candidate exists but we can't ask — don't auto-attach to an
        # existing export unattended; let the script's safe create-default
        # apply (it re-validates and never attaches to a foreign export).
        return None
    print(f"\nFound an existing CUR 2.0 export usable by tg: {candidate}")
    print("  Reuse it, or create tg's own export?")
    while True:
        ans = input("  [R] reuse (default)  [C] create: ").strip().lower()
        if ans in ("", "r", "reuse"):
            return "reuse"
        if ans in ("c", "create"):
            return "create"
        print("  please answer R (reuse) or C (create).")


def _run_addons(args, env) -> int:
    """#1075: CUR (tg-cur-athena) is a REQUIRED install step — the sole
    spend + discovery source (#720), same tier as the ALB/RDS/ECS the
    installer always creates. It is NOT skippable and a deploy failure
    is FATAL (returns non-zero), so a successful `tg install` always has
    a working CUR stack. tg-cur-deploy.sh is idempotent AND self-heals
    broken terminal states (ROLLBACK_COMPLETE / CREATE_FAILED → delete +
    recreate), so the "re-run resumes" promise is safe even after a
    failed prior CUR deploy.

    #1075 A.1 — `--local` is EXEMPT: the docker-compose dev path can't
    deploy the real us-east-1 CUR CFN stack, so for --local this stays a
    no-op (CUR is a cloud-install concern only)."""
    if getattr(args, "local", False):
        # Local docker-compose dev install: CUR is a cloud concern; skip
        # without failing (the only supported skip, and it's structural,
        # not an opt-out).
        return 0

    _check_no_stale_skip_cur()

    # #996: CUR deploy + verify still RUN, but on the happy path their
    # stdout (Athena SQL, the S3-inspect cmd, the "What happens next" /
    # "Verify wiring" / "Tear down" blocks, the raw
    # email/actual_usd/line_items dump) is CAPTURED, not streamed — so
    # the install's "✓ Install complete / URL / sign in" summary stays
    # the closing screen. The captured text is replayed ONLY on failure
    # (it's actionable then). The single user-facing CUR line ("spend
    # data in ~24h") is printed by cmd_install at the very end. The CUR
    # scripts are unchanged — run standalone they still print it all.
    print("\ndeploying CUR 2.0 + Athena (tg-cur-athena)…")
    rc, out = runner.run_captured(runner.CUR_DEPLOY_SH, env)
    if rc != 0:
        # #1075: FATAL. The core app may be up, but an install without
        # CUR is half-installed — no spend data anywhere (Activity AND
        # Cost Reports both read cur_user_spend, populated only by the
        # CUR→Athena sync) and the deny reconciler's billed-MTD caps
        # can't work. Surface the captured cause + the idempotent re-run
        # promise, and propagate non-zero so the install reports failure.
        print(out, file=sys.stderr)
        print("\ntg: CUR deploy failed. The core app is up, but tg "
              "install is NOT complete without cost reporting (CUR is "
              "the sole spend source). Fix the cause (see the error "
              "above) and re-run `tg install` — it resumes idempotently "
              "(and self-heals a broken CUR stack).", file=sys.stderr)
        return rc

    # OQ2: auto-verify after the default CUR deploy so a half-wired CUR
    # surfaces now, not after the 24-48h data wait. A verify problem is
    # a WARN, not a fail (CUR resources exist; wiring confirmation is
    # advisory). On success the verify output (incl. the raw result
    # table) is swallowed — the "~24h" summary line carries the only
    # message the installer-user needs; on a problem, replay it.
    rc_v, out_v = runner.run_captured(runner.VERIFY_CUR_SH, env)
    if rc_v != 0:
        print(out_v, file=sys.stderr)
        print("tg: CUR-wiring verify reported a problem (CUR "
              "stack is deployed; this is advisory). See the "
              "output above.", file=sys.stderr)
    return 0


def _dry_run(answers, login_on, args=None) -> int:
    local = getattr(args, "local", False)
    # #926: install is Cognito-only → always single-phase, so the
    # dry-run dumps the phase-2 env (login ON). The Okta two-phase
    # preview is gone with the install path.
    env = to_env(answers, phase=2)
    print(runner.render_confirm(answers, env, local=local))
    print("\n[--dry-run] would export:")
    for k in sorted(env):
        print(f"    {k}={env[k]}")
    if answers.get("cert_mode") == CERT_SELFSIGNED:
        print("    TG_CERT_ARN=<from tg-make-selfsigned-cert.sh at deploy>")
    if getattr(args, "local", False):
        print("\n[--dry-run] --local: would deploy the docker-compose dev "
              "stack via tg-local-install.sh (skips the ECS cert 3-way).")
    elif login_on:
        print("\n[--dry-run] login is Cognito, single-phase (#926): the "
              "installer stands up the Cognito pool after the ALB exists "
              "and derives the OIDC issuer / client id / redirect URI / "
              "secret from it, deploying with login ON in one pass (no "
              "redirect-URI pause). SAML/OIDC federation is configured "
              "AFTER install, not here.")
    # #1075: CUR is a REQUIRED part of a cloud deploy (no opt-out); only
    # --local skips it (no cloud CUR from a docker-compose dev install).
    if getattr(args, "local", False):
        print("\n[--dry-run] --local: CUR 2.0 + Athena is not deployed "
              "(cloud-install concern only).")
    else:
        print("\n[--dry-run] would then deploy CUR 2.0 + Athena "
              "(tg-cur-athena, the sole spend source — required) and "
              "verify its wiring.")
    print("\n[--dry-run] no resources created. Re-run without "
          "--dry-run to deploy.")
    return 0


def _maybe_selfsigned(answers, env) -> None:
    """Self-signed: run the helper (needs operator creds), thread the ARN."""
    if answers.get("cert_mode") != CERT_SELFSIGNED or env.get("TG_CERT_ARN"):
        return
    cn = answers.get("cert_cn") or f"tg-{answers['account_id']}.elb"
    print(f"\nGenerating self-signed cert (CN={cn})…")
    env["TG_CERT_ARN"] = runner.make_selfsigned(cn, env)
    print(f"  cert ARN: {env['TG_CERT_ARN']}")
    # Persist so phase 2 reuses the same ARN without re-importing.
    answers["cert_arn"] = env["TG_CERT_ARN"]
    answers["cert_mode_resolved_arn"] = env["TG_CERT_ARN"]
    config.save(answers, answers.get("account_id"))



def cmd_status(_args) -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    profile = os.environ.get("AWS_PROFILE")
    info = runner.describe_stack("tg-container-stack", region, profile)
    if not info:
        print("tg-container-stack: not found (not installed, or wrong "
              "profile/region).")
        return 1
    print(f"tg-container-stack: {info.get('Status')}")
    for out in info.get("Outputs") or []:
        key = out.get("OutputKey", "")
        if key in ("ApiPublicEndpoint", "AlbDnsName", "BootstrapAdminEmail"):
            print(f"  {key}: {out.get('OutputValue')}")
    return 0


def cmd_destroy(args) -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    local = getattr(args, "local", False)
    target = ("the local docker-compose stack (tg-local-destroy.sh)"
              if local else
              "the clean-slate verifier against tg-container-stack "
              "(+ ECR, VPC, RDS, ALB, ECS, custom-resource Lambdas/roles)"
              + (" AND the shared bedrock-layer stacks + tg-cur-athena "
                 "(--full)"
                 if args.full else ""))
    if args.dry_run:
        print(f"[--dry-run] tg destroy would run {target} in {region}.\n"
              "[--dry-run] nothing deleted. Re-run without --dry-run "
              "to tear down.")
        return 0
    if not args.non_interactive:
        try:
            ans = input("Tear down ALL tg- resources? type 'yes': ")
        except (KeyboardInterrupt, EOFError):
            print("\ntg: interrupted.", file=sys.stderr)
            return 130
        if ans.strip().lower() not in ("yes", "y"):
            print("aborted.")
            return 1
    # #559: thread --non-interactive into the bash teardown so its
    # Bedrock-logging prompts are suppressed (default = preserve).
    # The TG_PURGE_* purge switches stay env-only — the operator/CI
    # sets them explicitly; we never inject them here.
    env = {"TG_NONINTERACTIVE": "1"} if args.non_interactive else {}
    if local:
        return runner.run_local_destroy(env)
    rc = runner.run_destroy(env, full=args.full)
    # #922/#1075: CUR is a required install step now, so a --full
    # teardown must remove tg-cur-athena too (else it orphans the stack
    # + its S3 buckets). Idempotent — a no-op if CUR was never deployed
    # (e.g. a --local install). Only on --full, mirroring how
    # bedrock-layer stacks are --full-gated; a plain destroy preserves
    # CUR data like the bedrock layer.
    if rc == 0 and args.full:
        print("\n[--full] tearing down CUR (tg-cur-athena)…")
        cur_rc = runner.run_cur_destroy(env)
        if cur_rc != 0:
            print("tg: CUR teardown reported a problem — check "
                  "tg-cur-athena / its S3 buckets manually "
                  "(scripts/tg-cur-destroy.sh).", file=sys.stderr)
    return rc


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "install":
        return cmd_install(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "destroy":
        return cmd_destroy(args)
    return 2  # unreachable (subparsers required)


if __name__ == "__main__":
    sys.exit(main())
