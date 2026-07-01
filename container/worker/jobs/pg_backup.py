"""
pg_backup — daily pg_dump → S3.
Runs at 03:00 UTC. Skipped silently if PG_BACKUP_BUCKET not set.

#658 (security): RDS is the primary backup now; pg_backup is optional
belt-and-suspenders. Two hardening fixes here:
  - The DB password must never reach the process argv or any logged /
    API-queryable string. We pass the connection to pg_dump via libpq
    env vars (PGPASSWORD etc.) — never on argv — and scrub any leftover
    password/URL substring from stderr before it lands in the
    RuntimeError (which is persisted to JobRun.detail, readable via the
    admin API).
  - A pg_dump that returns 0 but produces an empty/truncated dump must
    NOT upload + report success. We assert the dump clears a small
    floor (a valid `--clean` dump always has a header) first.
"""
from __future__ import annotations
import gzip
import logging
import os
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote

import boto3

log = logging.getLogger("worker.pg_backup")

REGION = os.environ.get("AWS_REGION", "us-east-1")

# A valid `pg_dump --clean --if-exists` dump always emits a header
# (comments + SET statements + DROP/CREATE) well over this. A dump
# below it means pg_dump produced nothing useful even with rc=0
# (e.g. wrong db, permission quirk) — treat as failure, don't upload.
MIN_DUMP_BYTES = 512


def _libpq_env(database_url: str) -> dict:
    """Parse a postgres URL into libpq env vars so the password is
    NEVER placed on the pg_dump argv (where an error echo could leak
    it). Returns a dict to merge into the subprocess env."""
    parts = urlsplit(database_url)
    env: dict[str, str] = {}
    if parts.hostname:
        env["PGHOST"] = parts.hostname
    if parts.port:
        env["PGPORT"] = str(parts.port)
    if parts.username:
        env["PGUSER"] = unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    db = (parts.path or "").lstrip("/")
    if db:
        env["PGDATABASE"] = db
    return env


def _scrub(text: str, database_url: str) -> str:
    """Redact the DB password (and the full URL, which embeds it) from
    arbitrary text before it's logged or raised. Defense in depth — the
    libpq-env path already keeps the password off argv, but pg_dump
    could still echo a URL we hand it elsewhere, and we never want the
    secret in JobRun.detail."""
    if not text:
        return text
    out = text
    if database_url:
        out = out.replace(database_url, "[redacted-db-url]")
        parts = urlsplit(database_url)
        if parts.password:
            pw = unquote(parts.password)
            # Replace both the raw and URL-encoded forms.
            out = out.replace(pw, "[redacted]")
            if parts.password != pw:
                out = out.replace(parts.password, "[redacted]")
    return out


def run() -> str:
    database_url = os.environ.get("DATABASE_URL", "")
    backup_bucket = os.environ.get("PG_BACKUP_BUCKET", "")
    if not backup_bucket:
        log.info("PG_BACKUP_BUCKET not set — skipping backup")
        return "skipped (no bucket configured)"

    ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"/tmp/tg-pg-{ts}.sql.gz"
    key      = f"backups/tg-pg-{ts}.sql.gz"

    # Connection via libpq env vars — the password is NOT on argv.
    env = {**os.environ, **_libpq_env(database_url)}
    result = subprocess.run(
        ["pg_dump", "--clean", "--if-exists"],
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        # Scrub before the stderr text reaches JobRun.detail.
        stderr = _scrub(result.stderr.decode(errors="replace"),
                        database_url)
        raise RuntimeError(f"pg_dump failed: {stderr[:500]}")

    # rc=0 but empty/truncated → do NOT upload a useless "success".
    if len(result.stdout) < MIN_DUMP_BYTES:
        raise RuntimeError(
            f"pg_dump produced a suspiciously small dump "
            f"({len(result.stdout)} bytes < {MIN_DUMP_BYTES}) — "
            f"refusing to upload an empty backup"
        )

    with gzip.open(filename, "wb") as f:
        f.write(result.stdout)

    s3 = boto3.client("s3", region_name=REGION)
    s3.upload_file(filename, backup_bucket, key)
    os.unlink(filename)

    return f"uploaded {key} to s3://{backup_bucket}"
