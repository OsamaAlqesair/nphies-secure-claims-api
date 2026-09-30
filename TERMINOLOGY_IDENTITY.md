# Unversioned terminology identity

Identity is the exact (code_system_url, code) pair, across active, inactive and
soft-deleted records. Different systems may use the same code. Deletion/inactivation
does not release an identity. No version column or coverage constraint is added.

Migration 0005_terminology_identity refuses any duplicate pair (including historical
rows) or NULL identity. It never deletes, merges, normalizes or repairs rows.
After successful preflight it adds NOT NULL identity columns and the named unique
constraint uq_terminology_identity. Empty strings remain a separate validation
concern; this migration does not rewrite or reject existing non-NULL spellings.

PostgreSQL takes an ACCESS EXCLUSIVE table lock through migration commit, covering
preflight and DDL. Schedule a maintenance window: reads/writes can wait. Run a
separately authorized read-only data audit before deployment. Existing duplicates
or NULLs require reviewed remediation, not automatic cleanup. Downgrade removes
only this constraint and the NOT NULL requirements; rows remain intact.

Seed transactions remain atomic per source CodeSystem. An insertion constraint
conflict rolls back that file and is reported as failure, never as an insertion.
The ICD importer rolls back its entire import on a constraint conflict and clears
both committed and would-insert counters. Neither path silently overwrites a
concurrent winner. Re-run after reviewing failures; sequential exact reimports
remain idempotent. ICD advisory locks coordinate that importer only; the database
constraint is the protection against other writers.

Existing columns and valid writes remain compatible. Old writers can receive an
IntegrityError for a duplicate instead of creating one. Updated conflict reporting
should be deployed before enabling the constraint. create_all does not upgrade an
existing table. Import safety requires the migration on existing installations.

Tests in test_terminology_identity.py use SQLite plus, when available, a newly
created disposable local Docker container (postgres:16 by default; set
TEST_POSTGRES_IMAGE=postgres:17 to use that locally available image). They never use DATABASE_URL
or an existing PostgreSQL database for PostgreSQL testing. No image is downloaded.
PostgreSQL cases skip if the local daemon/image is unavailable. SQLite tests are
not evidence of PostgreSQL locking or concurrent-insert behavior.

Suggested isolated tests:
python -B -m pytest test_terminology_identity.py test_seed_nphies_data.py test_icd10_am.py -q -p no:cacheprovider

PostgreSQL-only concurrency and migration behavior must pass before deployment.
No production migration has been run as part of this implementation.
