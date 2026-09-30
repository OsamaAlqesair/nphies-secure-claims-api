"""Reserve unversioned terminology identities across all historical states."""

from alembic import context, op
import sqlalchemy as sa

revision = "0005_terminology_identity"
down_revision = "0004_terminology"
branch_labels = None
depends_on = None
NAME = "uq_terminology_identity"

# Fixed diagnostics deliberately omit catalog values and connection details.
PREFLIGHT = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM nphies_terminology
             WHERE code_system_url IS NULL OR code IS NULL) THEN
    RAISE EXCEPTION 'Terminology identity migration refused: NULL identities require review.';
  END IF;
  IF EXISTS (SELECT 1 FROM nphies_terminology
             GROUP BY code_system_url, code HAVING COUNT(*) > 1) THEN
    RAISE EXCEPTION 'Terminology identity migration refused: duplicate identities require review.';
  END IF;
END $$;
"""


def upgrade():
    offline = context.is_offline_mode()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Hold through commit: preflight and constraint creation see a stable table.
        op.execute("LOCK TABLE nphies_terminology IN ACCESS EXCLUSIVE MODE")
        op.execute(PREFLIGHT)
    elif offline:
        raise RuntimeError("Offline identity migration requires PostgreSQL.")
    else:
        table = sa.table(
            "nphies_terminology", sa.column("code_system_url"), sa.column("code")
        )
        if (
            bind.execute(
                sa.select(table)
                .where(
                    sa.or_(table.c.code_system_url.is_(None), table.c.code.is_(None))
                )
                .limit(1)
            ).first()
            is not None
        ):
            raise RuntimeError(
                "Terminology identity migration refused: NULL identities require review."
            )
        if (
            bind.execute(
                sa.select(table.c.code_system_url, table.c.code)
                .group_by(table.c.code_system_url, table.c.code)
                .having(sa.func.count() > 1)
                .limit(1)
            ).first()
            is not None
        ):
            raise RuntimeError(
                "Terminology identity migration refused: duplicate identities require review."
            )

    # create_all may already have installed the same constraint in a test database.
    constraints = (
        [] if offline else sa.inspect(bind).get_unique_constraints("nphies_terminology")
    )
    named = next((item for item in constraints if item["name"] == NAME), None)
    if named and set(named["column_names"]) != {"code_system_url", "code"}:
        raise RuntimeError(
            "Existing terminology identity constraint has unexpected columns."
        )
    with op.batch_alter_table("nphies_terminology") as batch:
        batch.alter_column("code_system_url", existing_type=sa.String(), nullable=False)
        batch.alter_column("code", existing_type=sa.String(), nullable=False)
        if not named:
            batch.create_unique_constraint(NAME, ["code_system_url", "code"])


def downgrade():
    # No rows are deleted; only the protections introduced here are removed.
    with op.batch_alter_table("nphies_terminology") as batch:
        batch.drop_constraint(NAME, type_="unique")
        batch.alter_column("code_system_url", existing_type=sa.String(), nullable=True)
        batch.alter_column("code", existing_type=sa.String(), nullable=True)
