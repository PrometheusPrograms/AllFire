# CI/CD Pipeline

## 1. Branch / environment strategy

```
feature/*  →  PR into staging  →  staging (Neon branch, Render preview, Vercel preview)
                                        │
                                   manual verification
                                        │
                              PR: staging → main  →  main / production
```

- Feature branches never touch `main` directly — everything flows through `staging` first.
- `staging` maps to its own Neon database branch (free, instant, copy-on-write from prod) —
  so staging tests run against realistic data without touching anything real.
- Promotion to `main` is a second, deliberate PR — not automatic. This is the actual
  answer to "staging before production": nothing reaches prod without a human looking at
  how it behaved on staging first.

## 2. What runs on every PR (quality + security gate)

All free, none dependent on GitHub's paid Advanced Security tier (which private repos don't
get for CodeQL/secret scanning):

| Check | Tool | Blocks merge on |
|---|---|---|
| Lint | `ruff` (Python), `eslint` (TS) | Any error |
| Format | `ruff format --check`, `prettier --check` | Any diff |
| Type check | `mypy`, `tsc --noEmit` | Any error |
| Unit + integration tests | `pytest`, `vitest` | Any failure |
| Coverage on money-math code | `pytest --cov=app/services` | Below threshold (start at 90%+ — this is the Kelly/ARORC/cost-basis code, it should be close to fully covered) |
| Dependency vulnerabilities | `pip-audit`, `npm audit` | Any high/critical |
| Secret scanning | `gitleaks` | Any detected secret, even in history of the diff |
| Migration safety | custom script, see §3 | Any undeclared destructive change |
| Build | `next build`, backend import smoke test | Any failure |

Example backend workflow:

```yaml
# .github/workflows/backend-ci.yml
name: Backend CI
on:
  pull_request:
    paths: ['backend/**']

jobs:
  quality-and-security:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: test
        ports: ['5432:5432']
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }

      - run: pip install -r requirements.txt -r requirements-dev.txt

      - name: Lint
        run: ruff check .
      - name: Format check
        run: ruff format --check .
      - name: Type check
        run: mypy app/
      - name: Dependency vulnerabilities
        run: pip-audit -r requirements.txt
      - name: Secret scan
        uses: gitleaks/gitleaks-action@v2
      - name: Migration safety check
        run: python scripts/check_migration_safety.py
      - name: Apply migrations to test DB
        run: alembic upgrade head
        env: { DATABASE_URL: postgresql://postgres:test@localhost/postgres }
      - name: Tests + coverage
        run: pytest --cov=app/services --cov-fail-under=90
```

## 3. Migration safety (backward compatibility + no data loss)

Two separate protections:

**a) Automated destructive-change check.** A CI step that scans new/changed Alembic
migration files for `drop_column`, `drop_table`, or a type change that narrows precision,
and fails the build unless the migration file contains an explicit
`# BREAKING: <reason>` comment. This forces a conscious decision, not an accidental one,
every time a migration could lose data.

**b) Expand/contract pattern for schema changes**, so the app is never broken mid-deploy —
this is what "backward compatible" actually means in practice for a database-backed app:

1. **Expand:** add the new column/table. Deploy. Old code ignores it; nothing breaks.
2. **Migrate:** backfill data into the new structure, deploy code that writes to both
   old and new.
3. **Cut over:** deploy code that reads only from the new structure.
4. **Contract:** once you're confident nothing depends on the old column, drop it in its
   own separate migration, explicitly marked `# BREAKING`.

This means a migration and a deploy are never required to land in the exact same instant —
there's always a window where both old and new code work against the current schema, so a
slow rollout or a rollback never leaves you in a broken state.

## 4. Promotion to production

1. PR from `staging` → `main`, after staging has been manually verified.
2. Before the migration runs against production: **create a Neon branch off the current
   prod state** as an instant rollback point (this is a one-command, few-second operation
   on Neon — effectively a free pre-deploy snapshot, on top of the independent nightly
   backup that already exists regardless).
3. Apply migrations to production.
4. Deploy backend (Render) and frontend (Vercel).
5. If something's wrong: Render and Vercel both support instant rollback to the previous
   deploy from their dashboards; the Neon branch from step 2 is the database rollback path.

## 5. Where this leaves the "never lose data" requirement

Four independent layers, so no single failure loses anything:

1. Expand/contract migrations — the app is never mid-broken by a schema change.
2. The pre-deploy Neon branch snapshot — instant rollback point around every prod deploy.
3. The nightly `pg_dump` to a second, independent location (from `ARCHITECTURE.md`) —
   protects against a provider-level failure, not just a bad deploy.
4. `trade_events`/`cost_basis` being append-only — even a bad write doesn't destroy prior
   history, it just adds a bad row you can identify and correct with a new row.
