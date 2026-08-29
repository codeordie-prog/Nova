# Local stack

Redis and Postgres running on your machine instead of Redis Cloud and Cloud SQL.

Only the infrastructure is local. The backend API, `do_work.py` and the MedGemma
gateway still run on the host under poetry, so reload and debugging are unchanged.

## Why

Before this, local development pointed at shared cloud infrastructure: `REDIS_URL`
at Redis Cloud, `POSTGRES_HOST` at the Cloud SQL public IP. Two people debugging
at once shared consumer groups, stream backlogs and one billing database — and
with `BILLING_ENFORCE=true` a local experiment moved balances that the admin
portal was reading. A local stack makes a botched run cost nothing and lets
`reset` be a real option.

## Bring it up

```bash
./dermascan/scripts/local_stack.sh up
```

That copies `.env.local.example` to `.env.local` on first run, starts Redis and
Postgres, waits for both to accept connections, creates `qutis_db`, and applies
every pending migration. Then start the app as usual:

```bash
poetry run uvicorn dermascan.backend.app.main:app --port 8088 --reload
poetry run python dermascan/scripts/do_work.py
```

| Command | What it does |
|---|---|
| `up` | Start services, wait for health, apply migrations |
| `down` | Stop the services, keep the data |
| `status` | What is listening, on which driver, and whether migrations are current |
| `migrate` | Apply pending migrations only |
| `reset` | Destroy all local data and re-migrate |
| `psql` | Shell on `qutis_db` |
| `redis` | `redis-cli` on the local instance |

## Docker or Homebrew

The script prefers Docker Compose (`docker-compose.yml`), because `redis:7-alpine`
and `postgres:16-alpine` are what staging runs. When Docker is not installed it
falls back to Homebrew services — a working stack beats a matching one you cannot
start — and warns about the version gap. `status` prints which driver is active.

Both drivers bind the same ports, 6379 and 5432, so one `.env.local` serves
either. That means only one can run at a time; `up` under Docker checks the ports
and tells you what to stop.

The Homebrew path also creates a `postgres` role with password `localdev`, because
Homebrew initialises the cluster with your login user as the only superuser while
`billing/db.py` builds its conninfo from `POSTGRES_USER` and refuses an empty
password even against a server that trusts loopback connections.

## How `.env.local` works

`bootstrap_env()` loads `.env.local` *before* `.env`. Every load is
`override=False`, so the first value seen wins: `.env.local` supplies the Redis
and Postgres pointers, and everything it does not mention — API keys, model
chains, Firebase credentials — still comes from `.env`. Your `.env` is never
touched, and deleting `.env.local` returns you to the cloud infrastructure.

`.env.local` is gitignored; `.env.local.example` is committed.

Note that `TEST_MODE=true` is *not* the local stack. It redirects `REDIS_URL` to
the shared cloud test instance and swaps in the test Telegram bot token, which is
the opposite of running in isolation. `.env.local.example` pins it to `false`.

## Billing on the local stack

`.env.local.example` ships with `BILLING_ENABLED`, `BILLING_METER_ENABLED` and
`BILLING_ENFORCE` all true — safe here, since the database is disposable.

Prices are not environment variables. Migrations `001` and `003` seed
`billing.price_list` with the credit prices, `usd_kes_rate`, the free-trial grant,
the top-up minimum and the hold ceilings, so a freshly migrated local database has
a complete rate card. Provider token prices are a separate thing: they live in the
`BILLING_TOKEN_RATES_USD` dict in `dermascan/billing/config.py` and are converted
to credits through the `usd_kes_rate` row at call time.

Top-ups need a Paystack `sk_test_` key in `PAYSTACK_SECRET_KEY`. The webhook's
source-IP allowlist is skipped for test keys, so a tunnel or a replayed `curl`
reaches `POST /v1/webhooks/paystack` without spoofing.

## Verifying

```bash
./dermascan/scripts/local_stack.sh status
poetry run pytest dermascan/tests/integration/test_billing_flow.py -q
```

The billing integration suite builds its own throwaway database on the same
server, so it exercises real Postgres rather than the running `qutis_db`.
