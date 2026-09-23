"""Track the terminology table, including databases populated by the seed script."""

from alembic import op
import sqlalchemy as sa

revision = "0004_terminology"
down_revision = "0003_audit_logs"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve an existing create_all/seed-created table and all its identifiers.
    op.create_table(
        "nphies_terminology",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code_system_url", sa.String(), nullable=True),
        sa.Column("code", sa.String(), nullable=True),
        sa.Column("display", sa.String(), nullable=True),
        sa.Column("definition", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        if_not_exists=True,
    )
    for column in ("id", "code_system_url", "code", "is_deleted"):
        op.create_index(
            f"ix_nphies_terminology_{column}",
            "nphies_terminology",
            [column],
            if_not_exists=True,
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_update ON nphies_terminology")
        op.execute(
            "CREATE TRIGGER audit_update BEFORE UPDATE ON nphies_terminology "
            "FOR EACH ROW EXECUTE FUNCTION nphies_audit_update()"
        )


def downgrade():
    raise RuntimeError("Terminology data cannot be removed by automatic downgrade.")
