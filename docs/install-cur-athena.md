# Install — CUR / Athena scaffolding

Optional. Adds Athena/Glue scaffolding *on top of* your
existing CUR 2.0 export so the **Cost Reports** page can
query per-user Bedrock spend.

If you don't already have a CUR 2.0 export with
`INCLUDE_IAM_PRINCIPAL_DATA=TRUE`, set that up first via the
AWS console or your existing IaC. TG doesn't manage the CUR
export itself.

## What it deploys

`scripts/tg-cur-deploy.sh` deploys the `tg-cur-athena` CFN
stack:

- S3 bucket `tg-cur-<acct>-<region>` (if you don't already
  have one) with the right CUR-writeable bucket policy
- Glue database (default `tg_cur`) and crawler
- Athena workgroup (`tg-cur-analytics`) with a results bucket
  and saved queries
- IAM grants for the api/worker tasks to read CUR + run
  Athena queries

## Run it

```bash
export AWS_PROFILE=tg-install
export AWS_REGION=us-east-1
bash scripts/tg-cur-deploy.sh
```

If you already have a CUR bucket, pass it via
`--cur-bucket-name` so the script wires the existing one
instead of creating a new bucket.

## Wait for data

CUR data is async — first Parquet file lands 24-48 hours
after enabling. The Glue crawler runs daily at 04:00 UTC and
auto-kicks once on stack create. Verify after the first
Parquet lands:

```bash
bash scripts/verify-cur.sh
```

## Wire the api container

After deployment the install script (or your tg-local-install
re-run) reads the stack outputs and writes them into
`.env.tg`:

- `ATHENA_RESULTS_BUCKET`
- `ATHENA_DATABASE`
- `ATHENA_WORKGROUP`
- `CUR_TABLE_NAME`

The api container runs Athena queries under its own task role
(`tg-app` on ECS, or the mounted `~/.aws/` profile on
local-compose) — #590 collapsed the former dedicated
`tg-ApiRunner` role into the single backend role, so there's no
separate role ARN to wire.

## Tear down

```bash
bash scripts/tg-cur-destroy.sh
```

Empties the buckets (`DeletionPolicy: Retain` keeps them
otherwise) and deletes the stack.

## Reference

- [INSTALL.md](../INSTALL.md) — main install path.
- [docs/admin-setup.md](admin-setup.md) — Cost Reports page
  in day-2 ops.
- AWS docs:
  <https://docs.aws.amazon.com/cur/latest/userguide/dataexports-create-cur.html>
