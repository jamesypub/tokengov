"""
tg-admin worker — replaces 4 EventBridge-triggered Lambdas.

Jobs:
  deny_reconciler     every 5 min  — cur_user_spend → IAM deny policy
  quota_reset         daily/monthly — zero counters
  pg_backup           daily        — pg_dump → S3
"""
import logging
import os
import signal
import sys
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from worker.jobs.deny_reconciler import run as run_reconciler
from worker.jobs.quota_reset import run_monthly
from worker.jobs.pg_backup import run as run_backup
from worker.jobs.github_sync import run as run_github_sync
from worker.jobs.pr_classify import run as run_pr_classify
from worker.jobs.pr_cost_rollup import run as run_pr_cost_rollup
from worker.jobs.jira_sync import run as run_jira_sync
from worker.jobs.governance_drift_check import (
    run as run_governance_drift_check,
)
from worker.jobs.service_account_monitor import (
    run as run_service_account_monitor,
)
from worker.jobs.cur_spend_sync import run as run_cur_spend_sync
from worker.job_runner import job

# #583: structured JSON logging (configurable via TG_LOG_FORMAT /
# TG_LOG_LEVEL). Replaces the plain basicConfig so worker logs are
# greppable + machine-parseable in CloudWatch / local docker logs.
from log_config import configure_logging
configure_logging()
# #587: attach the same request-context filter so worker logs share
# the request_id/caller fields (both "-" here — the worker has no
# HTTP request; the filter's ContextVar defaults keep it from
# erroring). Keeps the JSON shape identical across api + worker.
from api.log_context import install_request_context_filter
install_request_context_filter()
log = logging.getLogger("worker")


def main():
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        job("deny_reconciler", run_reconciler),
        IntervalTrigger(minutes=5),
        id="deny_reconciler",
        max_instances=1,
    )
    # #762: quota_monitor (80/90/100% email alerts) removed — the
    # in-app quota display is the alert surface and SMTP never sent
    # on deployed envs. deny_reconciler still enforces the cap.
    # #643: quota_reset_daily is retired (daily_tokens column gone —
    # windowed reads sum the usage_date range instead). The monthly
    # slot now runs the retention prune (delete day-rows older than
    # the retention window).
    scheduler.add_job(
        job("quota_reset_monthly", run_monthly),
        CronTrigger(day=1, hour=0, minute=0),
        id="quota_reset_monthly",
        max_instances=1,
    )
    scheduler.add_job(
        job("pg_backup", run_backup),
        CronTrigger(hour=3, minute=0),
        id="pg_backup",
        max_instances=1,
    )
    scheduler.add_job(
        job("github_sync", run_github_sync),
        IntervalTrigger(minutes=10),
        id="github_sync",
        max_instances=1,
    )
    scheduler.add_job(
        job("pr_classify", run_pr_classify),
        IntervalTrigger(minutes=30),
        id="pr_classify",
        max_instances=1,
    )
    scheduler.add_job(
        job("pr_cost_rollup", run_pr_cost_rollup),
        IntervalTrigger(minutes=30),
        id="pr_cost_rollup",
        max_instances=1,
    )
    jira_min = int(
        os.environ.get("TG_JIRA_SYNC_INTERVAL_MIN", "15"))
    scheduler.add_job(
        job("jira_sync", run_jira_sync),
        IntervalTrigger(minutes=jira_min),
        id="jira_sync",
        max_instances=1,
    )
    # #726 (#720 slice 4): pricing_proposer retired — CUR carries
    # billed spend directly (line_item_unblended_cost), so the
    # token→price estimation pipeline (#354) is no longer the
    # spend source. Job + module deleted.
    # #346: per-role budget monitor every 5 min, gated on
    # the EnableServiceAccountBudgets CFN param (default
    # off; tag-scoped IAM grants only attach when on).
    scheduler.add_job(
        job(
            "service_account_monitor",
            run_service_account_monitor,
        ),
        IntervalTrigger(minutes=5),
        id="service_account_monitor",
        max_instances=1,
    )

    # #649: daily governance-drift sweep, off-peak. Detect+alert
    # only (no IAM writes). Also runnable on-demand via
    # /api/jobs/run for an admin to re-check anytime.
    scheduler.add_job(
        job(
            "governance_drift_check",
            run_governance_drift_check,
        ),
        CronTrigger(hour=4, minute=30),
        id="governance_drift_check",
        max_instances=1,
    )
    # #724 (#720 slice 2): CUR → cur_user_spend, hourly (CUR
    # delivers <=3x/day). The new billed-spend source. Option C:
    # the sole billed-spend source (#725 retired metrics_aggregator).
    scheduler.add_job(
        job("cur_spend_sync", run_cur_spend_sync),
        IntervalTrigger(hours=1),
        id="cur_spend_sync",
        max_instances=1,
    )

    def _shutdown(sig, frame):
        log.info("shutting down scheduler")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("worker started — 10 jobs scheduled")
    scheduler.start()


if __name__ == "__main__":
    main()
