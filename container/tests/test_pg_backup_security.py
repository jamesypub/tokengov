"""
#658: pg_backup must never leak the DB password and never report
success on an empty dump.

Covers:
  - _libpq_env: URL → libpq env vars (password parsed out, so it can
    be passed via env, never argv).
  - failure path: pg_dump stderr containing the password → the raised
    RuntimeError (which lands in JobRun.detail, API-queryable) has the
    password REDACTED.
  - the password is never placed on the pg_dump argv.
  - empty/too-small dump (rc=0) raises instead of uploading.
  - happy path: a real-sized dump uploads and the password isn't in
    the returned detail.
"""
from __future__ import annotations
import subprocess

import pytest

import worker.jobs.pg_backup as pb

URL = "postgresql://tg:s3cr3t-pw@dbhost:5432/tg"


# ── _libpq_env (pure) ───────────────────────────────────────────

def test_libpq_env_parses_all_fields():
    env = pb._libpq_env(URL)
    assert env["PGHOST"] == "dbhost"
    assert env["PGPORT"] == "5432"
    assert env["PGUSER"] == "tg"
    assert env["PGPASSWORD"] == "s3cr3t-pw"
    assert env["PGDATABASE"] == "tg"


def test_libpq_env_urldecodes_password():
    env = pb._libpq_env("postgresql://u:p%40ss%2Fwd@h:5432/d")
    assert env["PGPASSWORD"] == "p@ss/wd"


def test_scrub_redacts_password_and_url():
    txt = f"connecting to {URL} failed: password 's3cr3t-pw' rejected"
    out = pb._scrub(txt, URL)
    assert "s3cr3t-pw" not in out
    assert URL not in out
    assert "[redacted]" in out or "[redacted-db-url]" in out


# ── failure path: password must be redacted in the raised error ──

def test_failure_stderr_redacts_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("PG_BACKUP_BUCKET", "tg-backups")

    captured = {}

    def fake_run(argv, capture_output, env):
        captured["argv"] = argv
        captured["env"] = env
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout=b"",
            # stderr echoes the password (the leak the fix prevents)
            stderr=f"pg_dump: error: connection to {URL} "
                   f"with password s3cr3t-pw failed".encode(),
        )

    monkeypatch.setattr(pb.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc:
        pb.run()
    msg = str(exc.value)
    assert "s3cr3t-pw" not in msg, "password leaked into error msg"
    assert URL not in msg
    # And the password was never on the argv.
    assert all("s3cr3t-pw" not in str(a) for a in captured["argv"])
    # It WAS passed via libpq env instead.
    assert captured["env"]["PGPASSWORD"] == "s3cr3t-pw"


# ── empty dump must not upload ───────────────────────────────────

def test_empty_dump_raises_not_uploads(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("PG_BACKUP_BUCKET", "tg-backups")

    monkeypatch.setattr(
        pb.subprocess, "run",
        lambda argv, capture_output, env: subprocess.CompletedProcess(
            argv, returncode=0, stdout=b"-- tiny", stderr=b""),
    )

    uploaded = {"called": False}

    class _FakeS3:
        def upload_file(self, *a, **kw):
            uploaded["called"] = True

    monkeypatch.setattr(pb.boto3, "client",
                        lambda *a, **kw: _FakeS3())

    with pytest.raises(RuntimeError, match="small dump|empty backup"):
        pb.run()
    assert uploaded["called"] is False, "must not upload empty dump"


# ── happy path: uploads, no secret in returned detail ────────────

def test_happy_path_uploads_and_no_secret_in_detail(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("PG_BACKUP_BUCKET", "tg-backups")

    big = b"-- PostgreSQL database dump\n" + b"X" * 1024
    monkeypatch.setattr(
        pb.subprocess, "run",
        lambda argv, capture_output, env: subprocess.CompletedProcess(
            argv, returncode=0, stdout=big, stderr=b""),
    )

    calls = {}

    class _FakeS3:
        def upload_file(self, fn, bucket, k):
            calls["bucket"] = bucket
            calls["key"] = k

    monkeypatch.setattr(pb.boto3, "client",
                        lambda *a, **kw: _FakeS3())
    monkeypatch.setattr(pb.os, "unlink", lambda p: None)

    detail = pb.run()
    assert calls["bucket"] == "tg-backups"
    assert calls["key"].startswith("backups/tg-pg-")
    assert "s3cr3t-pw" not in detail


def test_skips_when_no_bucket(monkeypatch):
    monkeypatch.delenv("PG_BACKUP_BUCKET", raising=False)
    out = pb.run()
    assert "skipped" in out
