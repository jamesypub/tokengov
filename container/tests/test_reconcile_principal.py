"""Unit tests for the synchronous per-principal reconcile:
deny_reconciler.reconcile_principal + the API _apply_now timeout guard.

reconcile_principal invokes the shared run() (single IAM writer) then
reports the TRUTHFUL post-apply state — never a false 'enforced', and
IDC stays pending. These tests stub run() + the IAM readback so the
state-decision logic is asserted without live AWS.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from worker.jobs import deny_reconciler as dr


def _user(email="dev@corp.com", governed=True, status="active",
          role_type="iam",
          arn="arn:aws:iam::123456789012:role/tg-consumer",
          cap=None):
    return SimpleNamespace(
        email=email, identity_key=email, governed=governed,
        status=status, role_type=role_type, principal_arn=arn,
        cap_usd=cap)


class TestReconcilePrincipalState:
    def test_force_blocked_enforced_when_deny_verified_attached(self):
        u = _user(status="force_blocked")
        with patch.object(dr, "run") as run, \
             patch.object(dr, "_principal_denied_now", return_value=True), \
             patch.object(dr, "deny_attached_to_role", return_value=True), \
             patch("boto3.client"):
            out = dr.reconcile_principal(db=None, user=u)
        run.assert_called_once()          # single-writer path invoked
        assert out["state"] == dr.APPLY_ENFORCED
        assert out["enforced"] is True
        assert out["denied"] is True

    def test_denied_but_attachment_unconfirmed_is_pending_not_enforced(self):
        # The deny SHOULD apply, but the readback couldn't confirm it →
        # pending, never a false enforced.
        u = _user(status="force_blocked")
        with patch.object(dr, "run"), \
             patch.object(dr, "_principal_denied_now", return_value=True), \
             patch.object(dr, "deny_attached_to_role", return_value=None), \
             patch("boto3.client"):
            out = dr.reconcile_principal(db=None, user=u)
        assert out["state"] == dr.APPLY_PENDING
        assert out["enforced"] is False
        assert out["denied"] is True

    def test_not_denied_is_allowed(self):
        # Unblock of an under-cap user → allowed (active).
        u = _user(status="active")
        with patch.object(dr, "run"), \
             patch.object(dr, "_principal_denied_now", return_value=False), \
             patch.object(dr, "deny_attached_to_role", return_value=False), \
             patch("boto3.client"):
            out = dr.reconcile_principal(db=None, user=u)
        assert out["state"] == dr.APPLY_ALLOWED
        assert out["enforced"] is False
        assert out["denied"] is False

    def test_unblock_still_over_cap_reports_denied(self):
        # Unblock cleared force-block, but the reconcile re-denied
        # because still over cap → truthful denied (not a false allowed).
        u = _user(status="active")
        with patch.object(dr, "run"), \
             patch.object(dr, "_principal_denied_now", return_value=True), \
             patch.object(dr, "deny_attached_to_role", return_value=True), \
             patch("boto3.client"):
            out = dr.reconcile_principal(db=None, user=u)
        assert out["denied"] is True
        assert out["state"] == dr.APPLY_ENFORCED

    def test_idc_is_pending_never_enforced(self):
        # tg can't AttachRolePolicy an AWSReservedSSO_* role — honest
        # state stays pending_idc even though the reconcile ran.
        u = _user(role_type="idc",
                  arn="arn:aws:iam::1:role/aws-reserved/sso.amazonaws.com/"
                      "AWSReservedSSO_Dev_abc")
        with patch.object(dr, "run") as run, \
             patch.object(dr, "_principal_denied_now", return_value=True):
            out = dr.reconcile_principal(db=None, user=u)
        run.assert_called_once()
        assert out["state"] == dr.APPLY_PENDING_IDC
        assert out["enforced"] is False

    def test_run_failure_degrades_to_failed_not_raise(self):
        u = _user(status="force_blocked")
        with patch.object(dr, "run", side_effect=RuntimeError("throttled")), \
             patch.object(dr, "_principal_denied_now", return_value=True):
            out = dr.reconcile_principal(db=None, user=u)
        assert out["state"] == dr.APPLY_FAILED
        assert out["enforced"] is False


class TestApplyNowGuard:
    def test_timeout_degrades_to_pending(self):
        import api.routes.users as users_mod

        def _slow(db, u):
            import time
            time.sleep(5)
            return {"state": "enforced", "enforced": True}

        with patch("worker.jobs.deny_reconciler.reconcile_principal",
                   side_effect=_slow), \
             patch.object(users_mod, "_SYNC_RECONCILE_TIMEOUT_S", 0.2):
            out = users_mod._apply_now(db=None, u=_user())
        assert out["state"] == "pending"
        assert out["enforced"] is False

    def test_exception_degrades_to_failed(self):
        import api.routes.users as users_mod
        with patch("worker.jobs.deny_reconciler.reconcile_principal",
                   side_effect=RuntimeError("boom")):
            out = users_mod._apply_now(db=None, u=_user())
        assert out["state"] == "failed"
        assert out["enforced"] is False

    def test_passthrough_of_real_state(self):
        import api.routes.users as users_mod
        with patch("worker.jobs.deny_reconciler.reconcile_principal",
                   return_value={"state": "enforced", "enforced": True,
                                 "denied": True, "detail": "ok"}):
            out = users_mod._apply_now(db=None, u=_user())
        assert out == {"state": "enforced", "enforced": True,
                       "denied": True, "detail": "ok"}
