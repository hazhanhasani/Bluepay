# BluePay v1.1.0 Operations Guide

## Startup safety and migrations

The service starts in this order:

1. Restore the encrypted SQLite snapshot when compatibility mode is active.
2. Run `alembic upgrade head`.
3. Run legacy additive migrations for installations predating Alembic.
4. Compare live tables and columns with SQLAlchemy metadata (Schema Guard).
5. Execute a database read/write readiness probe.
6. Load settings and appearance, then start Telegram and Callback workers.
7. Mark `/ready` healthy only after all required components are active.

A migration/schema failure keeps the process alive in maintenance mode so Railway logs remain available, but `/ready` returns HTTP 503. Railway therefore must not promote the broken deployment. Financial records are not deleted and automatic destructive downgrade is disabled.

## Railway health checks

`railway.json` uses:

```text
/ready
```

- `GET /health`: lightweight liveness; does not require business queries.
- `GET /ready`: database, migrations, schema and required worker readiness.
- `GET /health/details`: protected operational detail exposed by the application router.
- `GET /status`: public safe status page.
- `GET /metrics`: Prometheus text metrics without secrets.

After deploy, confirm `/ready` returns `200` before creating a live invoice.

## Telegram delivery mode

`TELEGRAM_MODE=auto` is the default:

- HTTPS Railway domain: Telegram Webhook is registered automatically.
- Local HTTP: Long Polling is used.
- `TELEGRAM_MODE=webhook`: force Webhook.
- `TELEGRAM_MODE=polling`: force Polling.

Webhook endpoint:

```text
/webhooks/telegram/{derived-secret}
```

The path secret is derived from `TELEGRAM_WEBHOOK_SECRET` or, when omitted, from `BOT_TOKEN`. It is never shown in public documentation.

## Database modes

- **SQLite compatibility mode:** omit `DATABASE_URL`; encrypted GitHub snapshot remains available when `GITHUB_TOKEN` is configured.
- **PostgreSQL production mode:** set Railway `DATABASE_URL`; PostgreSQL becomes the source of truth.

PostgreSQL uses pre-ping, bounded pooling, connection recycling and timeouts. Do not point a populated SQLite deployment to a new empty PostgreSQL database until data is exported and imported. Keep a verified backup before any switch.

### Moving SQLite to PostgreSQL

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source ./gateway.db \
  --database-url "$DATABASE_URL" \
  --confirm-empty-target
```

The importer refuses a non-empty target, copies records in foreign-key order, restores cyclic references and resets PostgreSQL sequences.

## Durable Callback Outbox

A paid invoice and callback event are committed in the same database transaction. The worker retries durable events after restart. Each attempt, status code, duration and final result are written to the callback event and payment Timeline. Failed events can be retried from the merchant portal.

## Payment Timeline

Every invoice can record events such as:

```text
invoice.created
payment.page_opened
sms.received
invoice.paid
callback.queued
callback.delivered / callback.failed
receipt.viewed
```

The authenticated API endpoint is:

```text
GET /api/v1/invoices/{payment_id}/timeline
```

The merchant portal also shows the recent Timeline for support investigations.

## Financial ledger and reports

`wallet_ledger` is append-only. Corrections are made by reversal entries. Do not edit or delete financial rows manually.

The merchant portal provides:

- 30-day gross payment and fee totals
- invoice CSV export
- wallet ledger CSV export
- Excel-compatible `.xls` statement with daily, monthly and store breakdowns
- printable financial statement and service invoice
- reconciliation cases
- callback delivery history

## Team access

Merchant owners can add Telegram users with one role:

- `finance`: financial reports and reconciliation
- `developer`: stores, API/Callback and Timeline
- `support`: invoices, Timeline and callback retry
- `viewer`: read-only operational view

The owner remains the only account that can add/remove team members. Team portal tokens are derived and revocable by disabling the membership.

## Secure SMS Forwarder devices

Register each device from the bot or merchant API. The device secret is shown only at creation/rotation.

Signature format:

```text
sha256=HMAC_SHA256(device_secret, timestamp + "." + raw_request_body)
```

Required headers after at least one secure device exists:

```http
X-BluePay-Timestamp: 1785600000
X-BluePay-Signature: sha256=...
```

The default replay window is 300 seconds (`SMS_HMAC_MAX_AGE_SECONDS`). Unknown, disabled, expired or invalidly signed devices are rejected and audited. Rotate or disable a device immediately when a phone is lost.

## Release staging, validation and rollback

Before GitHub publishing, BluePay validates:

- standard ZIP integrity and safe paths
- required project files
- Python syntax for every `.py` file
- `release.json` version
- Alembic revision when migrations are requested
- absence of databases, private keys and common secret files
- package SHA-256
- GitHub repository/branch push access

Admin release modes:

- **Staging:** publish to `RELEASE_STAGING_BRANCH` (default `bluepay-staging`).
- **Production:** publish to the Railway branch.
- **Rollback:** move the branch ref to the recorded previous commit after confirmation.

Workflow files are intentionally not included in the upload bundle because Fine-grained PATs without `Workflows: write` receive GitHub 403. A reviewed example is available at `ops/ci-workflow.example.yml`; copy it manually after granting the appropriate permission.

## Diagnostic center

The admin diagnostic center tests and reports:

- database connectivity and business query
- migration/schema readiness
- Telegram API access
- GitHub repository and branch permissions
- backup state
- callback worker state
- current deployment version and latest startup error

A JSON diagnostic report can be downloaded without exposing raw tokens.

## Sandbox

Sandbox records never reserve a real amount, select a live card, consume wallet credit or accept bank SMS. Simulations can produce `paid`, `failed` or `expired` and can exercise a Sandbox callback.

## Release checklist

1. Run `python scripts/validate_release.py`.
2. Run `pytest -q` with dependencies installed.
3. Test Alembic on a copy of the production database.
4. Publish to staging and verify `/ready`, bot commands, Sandbox and callback delivery.
5. Publish to production.
6. Verify `/ready`, `/metrics`, one Sandbox invoice and one callback test.
7. Keep the previous commit recorded for immediate rollback.
8. Build ZIP from repository root, remove caches/runtime data, run `unzip -t`, and publish SHA-256.

## Commerce scheduler and automation worker

Version 1.2.0 adds two independent durable workers:

- `commerce-scheduler`: creates subscription and scheduled invoices, dispatches queued payment requests and sends due reminders.
- `fulfillment-options`: executes connector calls, Telegram notifications, digital delivery, cashback and no-code automation actions with retry.

All business jobs are persisted before execution. A restart does not remove pending jobs. Failed jobs are placed in the admin inbox after their retry budget is exhausted.

### Permanent payment links

Public links use `/l/{slug}` and can collect customer data, apply discount and affiliate codes, create partial-payment invoices and emit analytics events. QR routes are generated locally and do not send payment data to third parties.

### Connector security

Connector secrets and headers are encrypted with the application encryption key. Final URLs are validated as public HTTPS destinations. Redirects are not followed. Use one least-privilege API key per connector and rotate it from the merchant options center.

### Commerce migration

Alembic revision `20260801_1200` creates the new commerce tables and adds invoice links for customer, campaign, A/B variant, discount, affiliate and subscription. The migration is additive and its downgrade intentionally retains financial and commerce history.


## Railway PostgreSQL production variables (v1.2.3)

Name the database service `Postgres`, then configure the BluePay service with:

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
DB_REQUIRE_POSTGRES=true
DB_CONNECT_RETRIES=30
DB_CONNECT_RETRY_SECONDS=3
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=300
GATEWAY_DISABLE_REMOTE_BACKUP=1
STARTUP_FAIL_OPEN=false
```

Do not add `DATABASE_URL` until the existing SQLite data has been copied. For
the one-time copy, add a temporary variable:

```env
MIGRATION_DATABASE_URL=${{Postgres.DATABASE_URL}}
```

Then run:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source /app/runtime/gateway.db \
  --database-url "$MIGRATION_DATABASE_URL" \
  --confirm-empty-target
```

After success, remove `MIGRATION_DATABASE_URL`, add the production
`DATABASE_URL` reference, and redeploy.
