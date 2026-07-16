"""DiagContext — the read-only context the engine passes to each check.

Bundles the resolved runtime env (role/policy names, Athena coordinates,
account/region/version) plus lazily-created boto3 clients and a DB
session factory. Checks read from it; they never mutate it.

The clients come from the app's shared `get_aws_session()` (the same
task-role cred chain the rest of the container uses — no new privilege),
and are memoized per service so repeated checks reuse one client. The
consumer-role and deny-policy names are IMPORTED from the deny_reconciler
(one definition of the TG_TOKEN_CONSUMER_ROLE_NAME → BEDROCK_ROLE_NAME →
"tg-consumer" fallback), never forked here.
"""
from __future__ import annotations

import os

# Single source of truth for the consumer-role + deny-policy names: the
# reconciler already resolves the env fallback chain at import; reuse it
# rather than re-deriving (keeps the engine and the enforcer in lock-step
# on which role/policy they're talking about).
from worker.jobs.deny_reconciler import (
    ROLE_NAME as _RECONCILER_ROLE_NAME,
    POLICY_NAME as _RECONCILER_POLICY_NAME,
)


class DiagContext:
    """Read-only context for a diagnostics run.

    Attributes:
        region, account_id, tg_version, environment — resolved env.
        consumer_role_name, deny_policy_name — the governed role + deny
            policy (imported from the reconciler).
        athena_workgroup, athena_database, cur_table_name — CUR coords.
    Methods:
        client(service) — a memoized boto3 client (read-only use).
        db() — a context-manager DB session (get_db()).
    """

    def __init__(
        self,
        *,
        session_factory=None,
        db_context=None,
        env=None,
        account_id=None,
    ):
        e = env if env is not None else os.environ
        self.region = e.get("AWS_REGION", "us-east-1")
        self.tg_version = e.get("TG_VERSION", "dev")
        self.environment = e.get("TG_ENVIRONMENT", "prod")
        self.consumer_role_name = _RECONCILER_ROLE_NAME
        self.deny_policy_name = _RECONCILER_POLICY_NAME
        self.athena_workgroup = e.get(
            "ATHENA_WORKGROUP", "tg-cur-analytics")
        self.athena_database = e.get("ATHENA_DATABASE", "tg_cur")
        self.cur_table_name = e.get("CUR_TABLE_NAME", "data")
        self._configured_account_id = e.get("AWS_ACCOUNT_ID", "")

        # account_id is the RESOLVED caller account (STS), filled by the
        # identity.caller check or the route; the configured value from
        # env is kept separately so identity.account-match can compare.
        self.account_id = account_id or self._configured_account_id

        # Injection seams (tests pass fakes; prod uses the app defaults).
        self._session_factory = session_factory
        self._db_context = db_context
        self._client_cache: dict[str, object] = {}

    @property
    def configured_account_id(self) -> str:
        """AWS_ACCOUNT_ID from env (what the deployment THINKS it is) —
        distinct from `account_id` (the STS-resolved caller account)."""
        return self._configured_account_id

    def client(self, service: str):
        """A memoized boto3 client for `service`. Uses the app's shared
        session by default; tests inject `session_factory`."""
        if service not in self._client_cache:
            if self._session_factory is not None:
                sess = self._session_factory()
            else:
                from api.aws_session import get_aws_session
                sess = get_aws_session()
            self._client_cache[service] = sess.client(
                service, region_name=self.region)
        return self._client_cache[service]

    def db(self):
        """A context-managed DB session (mirrors get_db()). Tests inject
        `db_context`."""
        if self._db_context is not None:
            return self._db_context()
        from db.session import get_db
        return get_db()
