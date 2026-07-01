"""Input validators — mirror the bash installer's own checks (#487).

Each returns an error string (shown to the user) or None when valid.
Kept in lock-step with scripts/tg-ecs-install.sh so the wizard rejects
the same bad input the installer would, before any mutation.
"""
from __future__ import annotations

import os
import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ACCOUNT_RE = re.compile(r"^\d{12}$")
# loose CIDR: a.b.c.d/n (the installer rejects 0.0.0.0/0 separately)
_CIDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
_ARN_RE = re.compile(r"^arn:aws[a-z-]*:acm:[^:]+:\d{12}:certificate/.+")
# #774: BYO VPC. vpc-<8 or 17 hex>; subnet-<8 or 17 hex>.
_VPC_RE = re.compile(r"^vpc-([0-9a-f]{8}|[0-9a-f]{17})$")
_SUBNET_RE = re.compile(r"^subnet-([0-9a-f]{8}|[0-9a-f]{17})$")


def email(val: str) -> str | None:
    return None if _EMAIL_RE.match(val) else f"not a valid email: {val!r}"


def admin_password(val: str) -> str | None:
    """#921: bootstrap-admin password policy — mirrors
    cfn/tg-cognito-pool.yaml (MinimumLength 12; lower+upper+number;
    RequireSymbols false). Empty is allowed (caller treats blank as
    "generate a random one + use forgot-password"). Validate here so
    a bad password fails clearly at the prompt, not as a cryptic
    Cognito error mid-install."""
    if val == "":
        return None
    if len(val) < 12:
        return "password must be at least 12 characters"
    if not any(c.islower() for c in val):
        return "password must include a lowercase letter"
    if not any(c.isupper() for c in val):
        return "password must include an uppercase letter"
    if not any(c.isdigit() for c in val):
        return "password must include a number"
    return None


def account_id(val: str) -> str | None:
    return None if _ACCOUNT_RE.match(val) else "must be a 12-digit account id"


def region(val: str) -> str | None:
    # CUR 2.0 only operates in us-east-1; the stack follows (#474 env).
    if not re.match(r"^[a-z]{2}-[a-z]+-\d$", val):
        return "not an AWS region (e.g. us-east-1)"
    return None


def require_ip_allowlist() -> bool:
    """Strict-allowlist policy gate (lock-step with tg-ecs-install.sh's
    TG_REQUIRE_IP_ALLOWLIST). DEFAULT OFF for the public product — a
    customer in their own environment may legitimately want open ingress
    protected by the login wall. Amazon/internal installs set the flag to
    restore the unconditional 0.0.0.0/0 reject (AppSec V2226500622 / #183).
    """
    return os.environ.get("TG_REQUIRE_IP_ALLOWLIST", "").lower() in (
        "1", "y", "yes", "true"
    )


def _login_off() -> bool:
    """True iff the login wall is explicitly disabled (TG_AUTH_REQUIRE_
    LOGIN=0). Open-all is refused when login is off — unauthenticated +
    world-open is the one genuinely-dangerous combo (mirrors the
    installer's hard-fail)."""
    return os.environ.get("TG_AUTH_REQUIRE_LOGIN") == "0"


def cidrs(val: str) -> str | None:
    """Comma-separated allowlist; reject empty and (conditionally)
    world-open. 0.0.0.0/0 handling is kept in lock-step with
    scripts/tg-ecs-install.sh:
      * TG_REQUIRE_IP_ALLOWLIST on (Amazon/internal posture) → always
        rejected (AppSec V2226500622 / #183).
      * Otherwise (default, customers) → allowed ONLY when the login wall
        is on; refused with the gate off (open + no-auth is forbidden).
    """
    parts = [c.strip() for c in val.split(",") if c.strip()]
    if not parts:
        return "at least one CIDR required (fail-closed: no empty allowlist)"
    if len(parts) > 4:
        return "at most 4 CIDR slots are wired into the stack"
    for c in parts:
        if not _CIDR_RE.match(c):
            return f"not a CIDR: {c!r} (e.g. 203.0.113.0/24)"
        if c == "0.0.0.0/0":
            if require_ip_allowlist():
                return (
                    "0.0.0.0/0 is rejected: TG_REQUIRE_IP_ALLOWLIST is on "
                    "(AppSec V2226500622 / #183). Use a real admin/VPN CIDR."
                )
            if _login_off():
                return (
                    "0.0.0.0/0 is not allowed with the login wall off "
                    "(TG_AUTH_REQUIRE_LOGIN=0) — unauthenticated + "
                    "world-open is the forbidden combo. Keep login on, or "
                    "use a real admin/VPN CIDR."
                )
            # login-gated open-all: permitted, login wall is the control.
    return None


def cert_arn(val: str) -> str | None:
    # #888: SHAPE check only — this runs before any AWS creds are
    # available, so it can't tell a real cert from a shape-valid but
    # nonexistent/placeholder ARN (e.g. .../certificate/dummy). The
    # EXISTENCE + ISSUED-status check is the installer's job
    # (tg-ecs-install.sh does `aws acm describe-certificate` in the
    # target account/region and fails fast). Kept in lock-step: this
    # gate catches a malformed ARN early; the installer catches a
    # well-formed-but-absent one before the deploy.
    return None if _ARN_RE.match(val) else "not an ACM certificate ARN"


def https_url(val: str) -> str | None:
    return None if re.match(r"^https://[^\s]+$", val) else "must be an https:// URL"


def yes_no(val: str) -> str | None:
    return None if val.lower() in ("y", "n", "yes", "no") else "answer y or n"


def is_yes(val: str) -> bool:
    return val.lower() in ("y", "yes", "true", "1")


def vpc_id(val: str) -> str | None:
    # #774: a BYO VPC id. Empty is allowed (means create-new).
    if not val:
        return None
    return None if _VPC_RE.match(val) else "not a VPC id (e.g. vpc-0abc123…)"


def subnet_ids(val: str) -> str | None:
    """#774: comma-separated subnet ids for a BYO VPC. Require ≥2
    (the RDS + ALB 2-AZ floor, #480) — AZ-distinctness is checked
    at deploy from the live subnets, not from the id strings."""
    parts = [s.strip() for s in val.split(",") if s.strip()]
    if len(parts) < 2:
        return "need ≥2 subnet ids across ≥2 AZs (comma-separated)"
    for s in parts:
        if not _SUBNET_RE.match(s):
            return f"not a subnet id: {s!r} (e.g. subnet-0abc123…)"
    return None
