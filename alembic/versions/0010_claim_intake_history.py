"""Immutable claim intake, validation attempt and event history.

No warehouse Claim conversion or existing data backfill is performed.
Triggers enforce append-only DML, not protection from privileged owners who
can disable triggers or perform destructive DDL.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_claim_intake_history"
down_revision = "0009_coverage_rule_current_identity"
branch_labels = None
depends_on = None


def _uuid_check(column, name, *, nullable=False):
    prefix = f"{column} IS NULL OR " if nullable else ""
    if op.get_bind().dialect.name == "postgresql":
        expression = f"{column} ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'"
    else:
        expression = (
            f"(length({column}) = 36 AND {column} = lower({column}) "
            f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
            f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
            f"AND length(replace({column}, '-', '')) = 32 "
            f"AND replace({column}, '-', '') NOT GLOB '*[^0-9a-f]*')"
        )
    return sa.CheckConstraint(prefix + expression, name=name)


def _object_check(column, name, *, original_text=False):
    if op.get_bind().dialect.name == "postgresql":
        value = f"{column}::jsonb" if original_text else column
        expression = f"jsonb_typeof({value}) = 'object'"
    else:
        expression = f"json_valid({column}) AND json_type({column}) = 'object'"
    return sa.CheckConstraint(expression, name=name)


def _timestamp(name):
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade():
    dialect = op.get_bind().dialect.name
    snapshot_json = sa.JSON().with_variant(JSONB(), "postgresql")
    hash_expression = (
        "canonical_input_hash ~ '^[0-9a-f]{64}$'"
        if dialect == "postgresql"
        else "length(canonical_input_hash) = 64 AND canonical_input_hash NOT GLOB '*[^0-9a-f]*'"
    )
    op.create_table(
        "claim_intakes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _timestamp("created_at"),
        sa.Column("idempotency_key", sa.String(36), nullable=False),
        sa.Column("canonical_input_hash", sa.String(64), nullable=False),
        sa.Column("original_request_snapshot", sa.Text(), nullable=False),
        sa.Column("validated_submission_snapshot", snapshot_json, nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.UniqueConstraint("public_id", name="uq_claim_intake_public_id"),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_claim_intake_owner_idempotency"
        ),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0 AND length(schema_version) <= 64",
            name="ck_claim_intake_schema_version",
        ),
        sa.CheckConstraint(hash_expression, name="ck_claim_intake_input_hash"),
        _uuid_check("public_id", "ck_claim_intake_public_uuid"),
        _uuid_check("idempotency_key", "ck_claim_intake_idempotency_uuid"),
        _object_check(
            "original_request_snapshot",
            "ck_claim_intake_original_object",
            original_text=True,
        ),
        _object_check(
            "validated_submission_snapshot", "ck_claim_intake_validated_object"
        ),
    )
    op.create_index(
        "ix_claim_intake_owner_created",
        "claim_intakes",
        ["owner_user_id", "created_at", "id"],
    )

    op.create_table(
        "claim_validation_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "intake_id",
            sa.Integer(),
            sa.ForeignKey("claim_intakes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        _timestamp("occurred_at"),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("operation_outcome_snapshot", snapshot_json, nullable=False),
        sa.Column("validation_report_snapshot", snapshot_json, nullable=False),
        sa.Column("validation_schema_version", sa.String(64), nullable=False),
        sa.Column("revalidation_idempotency_key", sa.String(36)),
        sa.UniqueConstraint("intake_id", "attempt_no", name="uq_claim_attempt_number"),
        sa.UniqueConstraint(
            "intake_id",
            "revalidation_idempotency_key",
            name="uq_claim_attempt_idempotency",
        ),
        sa.CheckConstraint("attempt_no > 0", name="ck_claim_attempt_number"),
        sa.CheckConstraint(
            "result IN ('PASSED', 'FAILED', 'UNAVAILABLE')",
            name="ck_claim_attempt_result",
        ),
        sa.CheckConstraint(
            "reason IN ('validation_passed', 'validation_failed', 'validation_unavailable')",
            name="ck_claim_attempt_reason",
        ),
        sa.CheckConstraint(
            "reason = 'validation_' || lower(result)",
            name="ck_claim_attempt_result_reason",
        ),
        sa.CheckConstraint(
            "length(trim(request_id)) > 0 AND length(request_id) <= 36",
            name="ck_claim_attempt_request_id",
        ),
        sa.CheckConstraint(
            "length(trim(validation_schema_version)) > 0 AND length(validation_schema_version) <= 64",
            name="ck_claim_attempt_schema_version",
        ),
        _uuid_check(
            "revalidation_idempotency_key",
            "ck_claim_attempt_idempotency_uuid",
            nullable=True,
        ),
        _object_check("operation_outcome_snapshot", "ck_claim_attempt_outcome_object"),
        _object_check("validation_report_snapshot", "ck_claim_attempt_report_object"),
    )
    for name, columns in (
        ("ix_claim_validation_attempts_actor_user_id", ["actor_user_id"]),
        ("ix_claim_validation_attempts_request_id", ["request_id"]),
        ("ix_claim_attempt_occurred", ["occurred_at"]),
    ):
        op.create_index(name, "claim_validation_attempts", columns)

    if dialect == "postgresql":
        detail_keys = "(details - 'attempt_no' - 'result') = '{}'::jsonb"
        detail_values = "(NOT (details ? 'result') OR (jsonb_typeof(details->'result') = 'string' AND details->>'result' IN ('PASSED', 'FAILED', 'UNAVAILABLE'))) AND (NOT (details ? 'attempt_no') OR (jsonb_typeof(details->'attempt_no') = 'number' AND details->>'attempt_no' ~ '^[1-9][0-9]*$'))"
    else:
        detail_keys = "json_remove(details, '$.attempt_no', '$.result') = '{}'"
        detail_values = "(json_type(details, '$.result') IS NULL OR (json_type(details, '$.result') = 'text' AND json_extract(details, '$.result') IN ('PASSED', 'FAILED', 'UNAVAILABLE'))) AND (json_type(details, '$.attempt_no') IS NULL OR (json_type(details, '$.attempt_no') = 'integer' AND json_extract(details, '$.attempt_no') > 0))"
    op.create_table(
        "claim_intake_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "intake_id",
            sa.Integer(),
            sa.ForeignKey("claim_intakes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column(
            "details", snapshot_json, nullable=False, server_default=sa.text("'{}'")
        ),
        _timestamp("occurred_at"),
        sa.UniqueConstraint("intake_id", "event_no", name="uq_claim_event_number"),
        sa.CheckConstraint("event_no > 0", name="ck_claim_event_number"),
        sa.CheckConstraint(
            "event_type IN ('intake.created', 'validation.completed')",
            name="ck_claim_event_type",
        ),
        sa.CheckConstraint(
            "reason IN ('intake_accepted', 'validation_completed')",
            name="ck_claim_event_reason",
        ),
        sa.CheckConstraint(
            "(event_type = 'intake.created' AND reason = 'intake_accepted') OR (event_type = 'validation.completed' AND reason = 'validation_completed')",
            name="ck_claim_event_type_reason",
        ),
        sa.CheckConstraint(
            "length(trim(request_id)) > 0 AND length(request_id) <= 36",
            name="ck_claim_event_request_id",
        ),
        sa.CheckConstraint(detail_keys, name="ck_claim_event_detail_keys"),
        sa.CheckConstraint(detail_values, name="ck_claim_event_detail_values"),
        _object_check("details", "ck_claim_event_details_object"),
    )
    for name, columns in (
        ("ix_claim_intake_events_actor_user_id", ["actor_user_id"]),
        ("ix_claim_intake_events_request_id", ["request_id"]),
        ("ix_claim_event_occurred", ["occurred_at"]),
    ):
        op.create_index(name, "claim_intake_events", columns)

    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_claim_intake_history_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'Claim intake history is append-only'; END; $$ LANGUAGE plpgsql"
        )
    for table in ("claim_intakes", "claim_validation_attempts", "claim_intake_events"):
        if dialect == "postgresql":
            op.execute(
                f"CREATE TRIGGER prevent_{table}_mutation BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_claim_intake_history_mutation()"
            )
            op.execute(
                f"CREATE TRIGGER prevent_{table}_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION reject_claim_intake_history_mutation()"
            )
        elif dialect == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER prevent_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Claim intake history is append-only'); END"
                )


def downgrade():
    raise RuntimeError("Claim intake history cannot be removed by automatic downgrade.")
