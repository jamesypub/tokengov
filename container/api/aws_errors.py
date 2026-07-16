"""
Shared helpers for translating boto3 ClientError shapes into
clean FastAPI responses.

Keeps the "container creds expired" case in one place so every
boto3-using route gets the same friendly 503 message instead
of a generic 500. After #116, boto3 inside the container
auto-refreshes via the native credential chain (mounted
~/.aws/ + AWS_PROFILE locally; task role on ECS; IRSA on
EKS), so this path almost never fires — but it's the right
shape for genuine cred-source failures (host IMDS lost,
SSO logged out on a dev mac, IRSA ServiceAccount
mis-annotated).
"""
from __future__ import annotations

# AWS error codes that mean "the container's STS session is
# no longer valid". boto3 surfaces these via:
#     err.response["Error"]["Code"]
EXPIRED_CRED_CODES = frozenset({
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "RequestExpired",
    "TokenRefreshRequired",
    "InvalidToken",
    # AWS returns this when the access key id is unknown
    # (e.g. the env still holds last hour's expired key
    # but a new session was minted, so the old AKID was
    # already retired). Treat it the same as expired.
    "UnrecognizedClientException",
})

# Body returned to the UI when creds are expired. Stable
# JSON shape so the frontend can detect `code == "creds_expired"`
# and show a custom toast/banner if it wants to.
EXPIRED_CRED_DETAIL = (
    "Container AWS credentials could not be refreshed. "
    "Check the host's credential source: instance role "
    "(local), task role (ECS), or IRSA (EKS). "
    "See docs/creds-refresh.md."
)


def is_expired_cred_error(err) -> bool:
    """True iff the given botocore ClientError is one of the
    expired-cred shapes. Safe on non-ClientError inputs."""
    try:
        code = err.response["Error"]["Code"]
    except (AttributeError, KeyError, TypeError):
        return False
    return code in EXPIRED_CRED_CODES
