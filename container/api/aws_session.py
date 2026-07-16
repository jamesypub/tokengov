"""
AWS session for the api container's CUR / Athena calls.

#590 (#566C — role consolidation): the api now queries Athena/CUR
under its OWN task-role credentials (`tg-app`), via boto3's native
credential chain — no assume-role hop. Previously (#358) the api
assumed a separate `tg-ApiRunner` role; the #566 epic collapsed the
backend to a single role (`tg-app`), so the CUR/Athena query perms
moved directly onto the task role and the assume-hop is gone.

Why a dedicated module still exists: analytics.py must NOT call
`boto3.client("athena")` ad-hoc — it should go through one cached
session so credential resolution is uniform and testable. The
human user's identity remains the audit trail's `actor`; the AWS
identity that runs the query is now uniformly the task role
(`tg-app` on ECS).

ECS path: the task role `tg-app` carries the Athena/CUR query
perms inline (cfn/tg-container-stack.yaml), so the native chain
resolves task-role creds with no plumbing.

Local-compose: the container has the installer's creds mounted at
`~/.aws/`; boto3's native chain picks them up directly (#116).
(Same mount as before — only the assume-role step was removed.)
"""
from __future__ import annotations
import logging
import os
from functools import lru_cache

import boto3

log = logging.getLogger("api.aws_session")


@lru_cache(maxsize=1)
def get_aws_session() -> boto3.Session:
    """Return a cached boto3.Session using the task role's own
    credentials (boto3 native chain). Cached for process
    lifetime; boto3 refreshes container/task-role creds
    transparently as expiry approaches."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    log.info(
        "aws_session: using task-role creds (region=%s)", region
    )
    return boto3.Session(region_name=region)


def reset_session_cache() -> None:
    """Test helper — drops the cached session so the next
    `get_aws_session()` call rebuilds it. Production callers
    should never need this."""
    get_aws_session.cache_clear()
