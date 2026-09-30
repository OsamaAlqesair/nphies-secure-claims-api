"""Append-only coverage mutation history; existing rules are not backfilled."""

from alembic import op
import sqlalchemy as sa

revision = "0006_coverage_rule_history"
down_revision = "0005_terminology_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coverage_rule_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "rule_id",
            sa.Integer(),
            sa.ForeignKey("diagnosis_service_rules.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("diagnosis_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("insurer_id", sa.Integer()),
        sa.Column("diagnosis_code", sa.String(), nullable=False),
        sa.Column("service_code", sa.String(), nullable=False),
        sa.Column("insurer_name", sa.String()),
        sa.Column("old_is_covered", sa.Boolean()),
        sa.Column("new_is_covered", sa.Boolean(), nullable=False),
        sa.Column("old_is_deleted", sa.Boolean()),
        sa.Column("new_is_deleted", sa.Boolean(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('CREATE', 'UPDATE', 'SOFT_DELETE', 'RESTORE')",
            name="ck_rule_history_action",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0 AND length(reason) <= 500",
            name="ck_rule_history_reason",
        ),
        sa.CheckConstraint(
            "(insurer_id IS NULL AND insurer_name IS NULL) OR (insurer_id IS NOT NULL AND insurer_name IS NOT NULL)",
            name="ck_rule_history_scope",
        ),
        sa.CheckConstraint("source = 'update_rule.py'", name="ck_rule_history_source"),
        sa.CheckConstraint(
            "(action = 'CREATE' AND old_is_covered IS NULL AND old_is_deleted IS NULL) OR (action <> 'CREATE' AND old_is_covered IS NOT NULL AND old_is_deleted IS NOT NULL AND ((action = 'UPDATE' AND old_is_deleted = false AND new_is_deleted = false AND old_is_covered <> new_is_covered) OR (action = 'SOFT_DELETE' AND old_is_deleted = false AND new_is_deleted = true AND old_is_covered = new_is_covered) OR (action = 'RESTORE' AND old_is_deleted = true AND new_is_deleted = false AND old_is_covered = new_is_covered)))",
            name="ck_rule_history_transition",
        ),
    )
    op.create_index(
        "ix_rule_history_rule_id_id", "coverage_rule_history", ["rule_id", "id"]
    )
    op.create_index(
        "ix_rule_history_occurred_at", "coverage_rule_history", ["occurred_at"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION reject_coverage_rule_history_mutation()
        RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Coverage rule history is append-only'; END;
        $$ LANGUAGE plpgsql""")
        op.execute(
            "CREATE TRIGGER prevent_rule_history_mutation BEFORE UPDATE OR DELETE "
            "ON coverage_rule_history FOR EACH ROW "
            "EXECUTE FUNCTION reject_coverage_rule_history_mutation()"
        )
        op.execute(
            "CREATE TRIGGER prevent_rule_history_truncate BEFORE TRUNCATE "
            "ON coverage_rule_history FOR EACH STATEMENT "
            "EXECUTE FUNCTION reject_coverage_rule_history_mutation()"
        )
    elif op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER prevent_rule_history_{operation.lower()} "
                f"BEFORE {operation} ON coverage_rule_history BEGIN "
                "SELECT RAISE(ABORT, 'Coverage rule history is append-only'); END"
            )


def downgrade():
    raise RuntimeError(
        "Coverage rule history cannot be removed by automatic downgrade."
    )
