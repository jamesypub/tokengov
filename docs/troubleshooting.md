# Troubleshooting — reading the logs

The `tg-admin` api + worker emit **structured JSON logs** to stdout
(#583/#587/#595). On ECS they land in CloudWatch Logs group
`/ecs/tg-container` (retention `LogRetentionDays`, default 7 days);
locally they're `docker compose logs api worker`.

Set `TG_LOG_FORMAT=plain` for human-readable lines in local dev;
ECS uses `TG_LOG_FORMAT=json`. `TG_LOG_LEVEL` (default `INFO`) sets
verbosity for both api + worker — set `DEBUG` to troubleshoot.

## Field set

Every JSON line carries:

| field | meaning |
|---|---|
| `ts` | ISO-8601 UTC timestamp |
| `level` | `INFO` / `WARNING` / `ERROR` / `DEBUG` |
| `logger` | emitting logger (`api.access`, `worker.job_runner`, …) |
| `msg` | the log message |
| `request_id` | per-HTTP-request correlation id (`-` off the request path); also returned to the client in the `X-Request-Id` response header |
| `caller` | the authenticated user email when known (`-` anonymous) |
| `run_id` | per-worker-job correlation id = the `JobRun` row id (`-` on the api side) |
| `exc` | stacktrace (on `ERROR` / exception lines) |

Event-bearing lines also carry an `event` field:
- `http_access` — one per request: `method`, `path`, `status`, `latency_ms`.
- `unhandled_exception` — an unhandled 500: `method`, `path` (+ `exc`).
- `job.start` / `job.ok` / `job.fail` — worker jobs: `job`, `duration_ms` (ok/fail), `status` (ok).
- `gate_reject` / `csrf_reject` — auth-gate / CSRF rejections: `path`, `method`, `reason`, `status`.

Secrets (tokens, API keys, `Authorization` values) are redacted to
`[REDACTED]` before any line is written. **Email is not redacted** —
it's the CUR per-user attribution key.

## CloudWatch Logs Insights queries

Run these in the console (Logs Insights → log group
`/ecs/tg-container`) or via `aws logs start-query`.

**1. Trace one request end-to-end** (the id a user quoted from a
`request_id` in a 500 body, or an `X-Request-Id` header):
```
fields ts, level, logger, msg, event, status
| filter request_id = "PASTE_REQUEST_ID_HERE"
| sort ts asc
```

**2. The last run of a specific job:**
```
fields ts, event, status, duration_ms, run_id
| filter logger = "worker.job_runner" and job = "deny_reconciler"
| sort ts desc
| limit 20
```

**3. All job failures today:**
```
fields ts, job, run_id, duration_ms, exc
| filter event = "job.fail"
| sort ts desc
```

**4. Recent application errors (any 5xx / exception):**
```
fields ts, logger, msg, request_id, path, exc
| filter level = "ERROR"
| sort ts desc
| limit 50
```

**5. Auth / CSRF rejections (who's getting bounced and why):**
```
fields ts, event, path, method, reason, status
| filter event = "gate_reject" or event = "csrf_reject"
| sort ts desc
| limit 50
```

**6. Slowest requests (latency outliers):**
```
fields ts, method, path, status, latency_ms, request_id
| filter event = "http_access"
| sort latency_ms desc
| limit 25
```

## Tips

- A user reports "it broke": ask for the **request_id** (it's in the
  `X-Request-Id` response header and the JSON body of any 500), then
  run query #1.
- A scheduled job misbehaves: query #2 by `job` name; the `run_id`
  matches the `JobRun` row in the Jobs page / `job_runs` table.
- Bump `TG_LOG_LEVEL=DEBUG` (CFN `LogLevel` param, or the env locally),
  redeploy/restart, reproduce, then set it back to `INFO`.
