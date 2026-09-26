# ICD-10-AM catalog readiness and local import

No approved dataset is included, generated or downloaded. Obtain a release
approved for your intended integration and confirm its licensing independently.

## Readiness

`diagnosis_catalog_readiness(session)` returns non-deleted coded row count and
active row count. Zero coded rows means missing. Inactive rows alone mean the
catalog is present, but those codes still cannot validate. Presence is not proof
of completeness, licensing, clinical accuracy or NPHIES release approval.

Both claim endpoints fail closed with HTTP 503 OperationOutcome when the
diagnosis catalog is missing. FHIR issue.code is not-found; the application
reason is icd10_am_catalog_missing in issue.details.coding, with diagnostics:
"ICD-10-AM terminology catalog is not loaded."

## Input contract

The explicit local JSON file must contain:
- system: the canonical ICD10_AM_SYSTEM URI from diagnosis_systems.py.
- version: optional nonempty release string (maximum 128 characters), parsed
  for dry-run inspection only. Explicit null is also accepted in dry-run.
- concepts: nonempty array of records with code, display and boolean active.

No ValueSet references or alternate coding systems are accepted. Regex checks
only syntax, not clinical authenticity. The operator must provide an approved
source. There is no fallback from a missing catalog to fixture data.

## Usage

```powershell
python import_icd10_am.py "C:\approved-data\icd10-am.json" --dry-run
python import_icd10_am.py "C:\approved-data\icd10-am.json" --apply
```

Dry-run is also the default when neither flag is provided. It validates the file,
compares existing rows, and reports would-insert, inserted, existing and failed.
It requires database access for comparison, but performs no inserts. PostgreSQL
dry-run uses a read-only transaction. Apply uses one transaction and counts
inserts only after commit. Existing differing records, deleted records, duplicate
identities cause the entire import to fail without writes.
No existing row is updated, deleted or restored.

Apply rejects any file containing a version field (including explicit null)
before opening a database session: "Version persistence is not yet supported."
Version metadata is never silently discarded. Dry-run accepts and validates
optional version metadata, but does not compare or persist releases. Do not strip
release metadata from a licensed dataset merely to bypass this restriction.

## Schema compatibility

No version column or new migration is required. The importer and readiness checks
use the existing terminology schema at migration 0004_terminology. Version storage,
release selection and terminology uniqueness semantics are deferred until the
approved dataset and release policy are known. Apply currently supports only
approved inputs without version metadata.

## Test-only data

test_icd10_am.py writes synthetic labels and test records to pytest temporary
directories and uses in-memory SQLite. These are not an official ICD-10-AM
dataset and must not be used as clinical reference data. No fixture is placed in
nphies_data or discovered by seed_nphies_data.py.

## Operational limits

The importer serializes its own PostgreSQL apply runs with a transaction-scoped
advisory lock. Other writers must honor that lock; there is no database uniqueness
constraint yet. No production seed, database or coverage changes are part of this
task.
