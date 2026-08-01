# BluePay Enterprise Operations

## Database modes

- **SQLite compatibility mode:** without `DATABASE_URL`; encrypted GitHub snapshot remains available when `GITHUB_TOKEN` is configured.
- **PostgreSQL production mode:** set Railway `DATABASE_URL`; GitHub database snapshots are disabled and PostgreSQL becomes the source of truth.

Do not point a populated SQLite deployment to a new empty PostgreSQL database until the data has been exported and imported. Keep a verified backup before any database switch.

## Durable Callback Outbox

A paid invoice and its callback event are committed in the same database transaction. The worker sends immediately, then after 30 seconds, then after 5 minutes. Each attempt has a 10-second timeout. HTTP 2xx succeeds; redirects are not followed. Failed events can be retried from the merchant portal or admin center.

## Financial ledger

`wallet_ledger` is append-only. Corrections are made by reversal entries. Do not edit or delete financial rows manually. The current wallet balance is protected with row locking when fees are reserved, released or collected.

## Sandbox

Sandbox records live in `sandbox_invoices`; they never reserve a real amount, select a card, consume wallet credit or accept bank SMS. Simulations can produce paid, failed or expired states. Paid simulations emit `sandbox.invoice.paid`.

## Monitoring

- `/health`: readiness, database mode and callback queue counts
- `/status`: public safe status page
- `/metrics`: Prometheus text metrics without account secrets
- `SENTRY_DSN`: optional exception reporting

## Release checklist

1. Run `python scripts/validate_release.py`.
2. Run `pytest -q` with dependencies installed.
3. Verify migrations on a copy of the production database.
4. Build ZIP from repository root, excluding caches and runtime data.
5. Run `unzip -t` and publish the SHA-256 checksum.
6. Deploy, verify `/health`, create a Sandbox invoice and test Callback delivery.

## Moving an existing SQLite installation to PostgreSQL

The switch is intentionally not automatic because silently pointing a populated deployment at an empty database would hide existing financial data. Restore/download the current `gateway.db`, create an empty PostgreSQL database, then run:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source ./gateway.db \
  --database-url "$DATABASE_URL" \
  --confirm-empty-target
```

The importer refuses a non-empty target, copies records in foreign-key order, restores cyclic references and resets PostgreSQL sequences. Verify record counts and Sandbox/Callback behavior before changing the production service variable.
