# NPHIES Claim Pre-Validation Gate

FastAPI, Pydantic V2 and SQLAlchemy/PostgreSQL prototype for exact financial validation
and configurable coverage checks before downstream NPHIES integration.
This accepts a simplified DTO, not a complete FHIR Claim or submission bundle.

## PostgreSQL setup

Python 3.11+ and Docker Compose are required. Local .env contains generated
credentials and is ignored by Git. Use .env.example on another machine and set
both POSTGRES_PASSWORD and the matching DATABASE_URL. Do not commit credentials.

```bash
python -m pip install -r requirements-dev.txt
python configure_auth.py
docker compose up -d --wait
python -m alembic upgrade head
python migrate_sqlite.py --source rules_engine.db
python -m uvicorn main:app --reload
```

Stop application writes during import. The importer requires empty legacy target
tables, preserves IDs and NULL rules, resets PostgreSQL sequences, verifies counts,
and commits atomically. SQLite is opened read-only and remains the rollback source.
Import timestamps record import time where the source has no audit history.
Do not run seed.py before an import. For a fresh demo without legacy data, run
`python seed.py` after migrating instead; seeding is now non-destructive and idempotent.
Never point Alembic at the old SQLite database. SQLite is supported only with
APP_ENV=test and an explicit test DATABASE_URL. No automatic schema changes occur
at application startup. Docker publishes PostgreSQL on localhost only.

- Scalar: http://127.0.0.1:8000/docs
- OpenAPI: http://127.0.0.1:8000/openapi.json

### Warehouse scope and preservation

Patient, Organization, Practitioner, Coverage, Encounter and Claim are normalized
FHIR R4 projections. The initial Claim stores one primary coverage, encounter and
practitioner; full repeating FHIR insurance, item.encounter and careTeam structures
remain in the optional JSONB resource. These models do not establish full NPHIES
profile conformance. The validation endpoint remains read-only; storing claims is
not automatically enabled by this schema change.

Composite foreign keys enforce patient consistency across claim, coverage and
encounter, and payor consistency between claim and coverage. Legacy InsuranceCompany
IDs are retained and can be explicitly linked to Organization records.

All model tables inherit is_deleted, created_at and updated_at. ORM reads hide
soft-deleted rows; use a fresh session with execution_options(include_deleted=True)
for administrative reads. Session.delete() becomes a soft delete, bulk ORM DELETE
is blocked, and restore() makes a row visible again. Relationships never cascade
physical deletes. These controls do not protect raw SQL or privileged database users.

PostgreSQL update triggers preserve created_at and refresh updated_at, including
raw SQL updates. Timestamps are audit metadata, not an immutable change-history
log. Soft deletion does not cascade to related resources.

The initial Alembic revision is a frozen schema snapshot. Destructive downgrade is
blocked. Keep database backups and do not use docker compose down -v on valued data.

## JWT authentication and roles

Run migrations, then create the first account locally. No default account or
password is provisioned:

```bash
python configure_auth.py
python -m alembic upgrade head
python manage_users.py create --role admin
```

The CLI prompts for username and a hidden, confirmed 12-128 character password.
Use `python manage_users.py create --role provider` for a provider account.
Account management is local-only: reset-password, set-role, disable, enable and
revoke actions invalidate previously issued tokens. Run `--help` for options.
Passwords are stored as Argon2id hashes; no password is accepted on the command line.

POST /auth/login accepts a JSON object containing username and password. It
returns access_token, token_type=bearer and expires_in. Supply the token via the
Authorization header using the Bearer scheme. Scalar exposes BearerAuth in its
Authentication controls. Never put tokens or passwords in URLs or log bodies.

- POST /process-claim: admin or provider.
- GET /auth/me: any active authenticated account.
- GET /auth/users: admin only, paginated; never returns hashes.
- Missing/invalid/expired/revoked credentials: 401 with a Bearer challenge.
- Valid identity with insufficient role: 403.
- Authentication errors use FHIR OperationOutcome.

SECRET_KEY is read from the project's ignored .env with no fallback. The setup
command creates 64 random bytes encoded as URL-safe text, preserving an existing
key. Application startup fails if the key is missing or too short. Configure
JWT_ISSUER and JWT_AUDIENCE in .env; tokens expire after ACCESS_TOKEN_MINUTES
(default 15, permitted 1-30). Verification pins HS256 and validates issuer,
audience, expiration, not-before, subject and token version. Roles and active
status are reloaded from PostgreSQL on every authenticated request.

Five failed attempts lock an existing account for 15 minutes; counters are stored
in PostgreSQL and serialized with row locks. Unknown, disabled and locked users
receive the same generic response. Add gateway-level request/body/concurrency
limits for distributed abuse protection. Use HTTPS outside loopback development,
restrict .env filesystem access, and configure proxy forwarding only for trusted
proxies. Rotating SECRET_KEY invalidates all outstanding tokens; restart every
worker together after rotation. Tokens are short-lived; no refresh-token flow is
implemented. .env.example contains configuration names, never the signing key.

Existing claim tests authenticate using temporary database users and real tokens.
Security tests cover login, expiry, signature/issuer/audience checks, RBAC,
lockout, disabled/deleted users, revocation and secret-configuration failures.

## Request contract

POST /process-claim with Content-Type application/json:

```json
{
  "diagnosis_code": "G43",
  "service_code": "70450",
  "insurer_id": null,
  "billed_amount": "500.00",
  "allowed_amount": "400.00",
  "copay": "50.00",
  "net_payable": "350.00"
}
```

Amounts are SAR, non-negative and finite, with at most 12 integer digits and two
fractional digits. Send decimal strings (recommended) or JSON integers. JSON
floating-point numbers, exponent notation, excess precision and unknown fields
are rejected. Insurer IDs must be positive integers; omitted or null uses global
rules. Diagnosis and service codes must exist in the database master data.

**Compatibility change:** use "400.00" or 400, not 400.0. Successful responses
now return net_payable as a two-decimal string, preserving monetary precision.

## Financial validation

Pydantic enforces before entering the endpoint:

- 0 <= copay <= allowed_amount <= billed_amount
- net_payable == allowed_amount - copay, without rounding or tolerance.

Money is represented as Decimal. Reconciliation uses integer halalas derived
exactly with Decimal.as_integer_ratio(), independent of Decimal context precision.
A one-halala mismatch is rejected.

## Coverage precedence

1. Match diagnosis and service, then select the requested insurer's rules.
2. Use global rules (insurer_id IS NULL) only if no insurer-specific rules exist.
3. Within the selected scope, any denial wins conflicting or duplicate rules.
4. If no applicable rule exists, deny by default.

A specific approval overrides a global denial; a specific denial overrides a
global approval. Rules for other insurers, diagnoses and services do not apply.
Duplicate records are not deleted; the verdict is independent of row order.

## Responses

Approval: HTTP 200, application/json. With the development seed data:

```json
{
  "status": "approved",
  "message": "Claim approved for CT Scan Head with diagnosis Migraine.",
  "net_payable": "350.00"
}
```

Financial, structural and medical rule rejections: HTTP 422.
Unknown diagnosis/service master code: HTTP 400.
Both return application/fhir+json and an OperationOutcome:

```json
{
  "resourceType": "OperationOutcome",
  "issue": [{
    "severity": "error",
    "code": "invariant",
    "diagnostics": "Financial math error. net_payable must equal allowed_amount - copay exactly."
  }]
}
```

Field-validation responses omit rejected input, exception contexts and arbitrary
unknown property names. These results are not NPHIES acceptance.

## Active code

- schemas/claim.py: money parsing, Pydantic invariants and response models.
- services/coverage.py: insurer/global rule resolution.
- main.py: FastAPI, Scalar, database dependency and error translation.
- models.py: SQLAlchemy master data and rule tables.
- test_main.py: financial boundaries, errors, precedence and fallback tests.
- test_scalar_docs.py: Scalar integration and claim regression checks.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests create isolated in-memory SQLite databases and override get_db; they do not
seed or modify rules_engine.db. Cases cover one-halala mismatches, 0.30 minus 0.10,
maximum amounts, low Decimal precision, invalid types, non-finite/negative money,
missing fields, malformed JSON, unknown master codes, conflicting rules in both
insertion orders, insurer overrides and global fallback.

## Scope

Passing tests verify these behaviors, not complete clinical, NPHIES-profile,
security or operational compliance. Authoritative terminology,
complete NPHIES profiles, insurer eligibility and submission are outside this
prototype. The active endpoint has no patient-ID or line-item contract.
Application-defined coverage policies require domain review before real use.

## Scalar browser compatibility

The Python integration is pinned to `scalar-fastapi==1.9.0`; the independently
loaded JavaScript bundle is pinned to `@scalar/api-reference@1.68.0` in `main.py`.
Do not replace its URL with an unversioned CDN URL without browser testing.

On this project, JavaScript 1.69.0 reproduced an empty Test Request client when
loading or refreshing `/docs#POST/process-claim`. Opening `/docs` without that
fragment worked. Selecting Process Claim in the empty client did not recover it.
The same direct-link test worked on 1.68.0 with the same OpenAPI content.
This is a locally reproduced UI regression, not an identified upstream source fix.

Docs are served with `Cache-Control: no-store`. The request server follows the
current origin and proxy root path. The Scalar compatibility pin is independent of claim validation.

Before upgrading Scalar, run `python -m pytest -q`, then verify in a browser:

1. Open `/docs#POST/process-claim` directly and click **Test Request**.
2. Refresh that page and open **Test Request** again.
3. Confirm the editable JSON body and Send button are present.
4. Submit synthetic data for approval, financial rejection, and medical denial.

`test_scalar_docs.py` checks documentation wiring and claim behavior using an
isolated in-memory database. These Python tests do not execute Scalar JavaScript;
the direct-link and refresh checks above remain required browser checks.


## Database audit events

`audit_logs` records login successes/failures, invalid authentication, denied roles,
and claim approvals/rejections (including financial/structural validation errors).
Each row contains UTC timestamps, actor user ID when known, action/outcome/reason,
HTTP status, server-generated request ID, route and connection peer IP. Responses
include X-Request-ID for correlation. Passwords, JWTs, usernames, patient data,
claim amounts and request/response bodies are never stored in these events.

Login events commit atomically with lockout counters. Claim decisions commit their
audit record before responding. Exception-handler events use a fresh transaction
so failed request work cannot roll them back. Audit persistence errors fail the
request rather than silently reporting success without a log.

ORM mutation is blocked; PostgreSQL triggers reject UPDATE, DELETE and TRUNCATE.
A database owner can disable triggers, so this is not tamper-proof storage against
privileged administrators. No public audit-read endpoint or automatic retention
purge is enabled. Restrict SQL access to authorized administrators. This covers
current API actions; local account-management CLI events are not included.
