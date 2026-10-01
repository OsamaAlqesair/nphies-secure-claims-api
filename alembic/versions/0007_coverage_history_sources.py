"""Allow direct coverage service events without changing rules or existing history."""

from alembic import context, op
import sqlalchemy as sa

revision = "0007_coverage_history_sources"
down_revision = "0006_coverage_rule_history"
branch_labels = None
depends_on = None


def _replace_source_constraint(expression):
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot ALTER a CHECK. Batch copying retains every history column,
        # constraint and index; reinstall its append-only triggers after the copy.
        with op.batch_alter_table("coverage_rule_history", recreate="always") as batch:
            batch.drop_constraint("ck_rule_history_source", type_="check")
            batch.create_check_constraint("ck_rule_history_source", expression)
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER prevent_rule_history_{operation.lower()} "
                f"BEFORE {operation} ON coverage_rule_history BEGIN "
                "SELECT RAISE(ABORT, 'Coverage rule history is append-only'); END"
            )
    else:
        op.drop_constraint(
            "ck_rule_history_source", "coverage_rule_history", type_="check"
        )
        op.create_check_constraint(
            "ck_rule_history_source", "coverage_rule_history", expression
        )


def upgrade():
    _replace_source_constraint("source IN ('update_rule.py', 'coverage_mutations.py')")


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError(
            "History source downgrade requires online inspection; history must be preserved."
        )
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Prevent an insert racing the preflight check and constraint replacement.
        op.execute("LOCK TABLE coverage_rule_history IN ACCESS EXCLUSIVE MODE")
    if (
        connection.scalar(
            sa.text(
                "SELECT 1 FROM coverage_rule_history WHERE source <> 'update_rule.py' LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "Cannot downgrade history sources while newer-source events exist; "
            "history must be preserved."
        )
    _replace_source_constraint("source = 'update_rule.py'")
