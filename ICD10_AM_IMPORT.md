# ICD-10-AM catalog readiness and local import

No source or generated catalog is bundled. Confirm licensing for each intended
destination before transferring source or normalized content. The locally reviewed
Tenth Edition reconstruction is a candidate membership list, not an official
electronic assignability list or a complete official description set.

## Readiness

`diagnosis_catalog_readiness(session)` selects the one ACTIVE ICD-10-AM catalog
whose provenance matches a code-reviewed manifest and whose persisted import
count/hash and validation/activation evidence are complete. It performs no entry
scan. Legacy `nphies_terminology` rows never satisfy readiness.

Migration `0011_terminology_catalogs` adds `terminology_catalogs` and
`terminology_catalog_entries`; it leaves existing terminology and coverage IDs
untouched. Entry identity is `(catalog_id, code)`, so editions coexist. A partial
unique index permits only one ACTIVE catalog per family. Database triggers seal
entries at validation, prohibit catalog deletion/provenance mutation, enforce
transition/count rules, and prohibit PostgreSQL TRUNCATE. Downgrade refuses any
persisted catalog history.

Lifecycle: IMPORTING -> VALIDATED -> ACTIVE -> SUPERSEDED; IMPORTING may instead
become FAILED. Superseded catalogs remain immutable and queryable. Activation is
separate from import. Repeated identical imports return the existing catalog;
changed content under the same source/edition/parser identity conflicts. A single
import transaction verifies persisted rows before sealing. PostgreSQL transaction
advisory locks serialize family imports/activations, backed by unique constraints
and parent row locks for entry writes.

The importer verifies exact manifest metadata, source hash, parser/schema version,
canonical unique leaves, count, and normalized content hash before opening a
database session. Artifact SHA-256 hashes the metadata-plus-entries envelope;
content SHA-256 hashes normalized entries; import SHA-256 is independently computed
from persisted entries. The source SHA-256 identifies the original bytes.
Display ambiguity is retained without rejecting code membership.

```powershell
python manage_icd_catalog.py import-source "C:\approved-local\1015865188-ICD-10-AM-Tabular-List.txt"
python manage_icd_catalog.py import-source "C:\approved-local\1015865188-ICD-10-AM-Tabular-List.txt" --apply
python manage_icd_catalog.py activate 1
```

The first command is a database-free dry run. Apply imports but does not activate.
Use the returned catalog ID for activation. These are administrative commands,
not public API endpoints. Never bundle local ICD sources or generated artifacts
with the deployment. Other editions require a separately reviewed manifest.

Each evaluator pins one catalog for the request. Report v2 stores its identity,
edition and provenance hashes, including negative membership decisions. Immutable
intake attempts retain that snapshot; v1 reports remain readable without invented
catalog provenance.

Both claim endpoints fail closed with HTTP 503 OperationOutcome when the
diagnosis catalog is missing. FHIR issue.code is not-found; the application
reason is icd10_am_catalog_missing in issue.details.coding, with diagnostics:
"ICD-10-AM terminology catalog is not loaded."

## Legacy unversioned importer (does not establish readiness)

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

## Legacy schema compatibility

No version column is required. Migration 0005_terminology_identity adds non-null
identity fields and full-history system/code uniqueness; it must be applied after
a separately authorized duplicate/NULL audit. See TERMINOLOGY_IDENTITY.md.
The legacy importer still accepts only inputs without version metadata. Use the
separate reviewed catalog importer above for versioned runtime membership.

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
