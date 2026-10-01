"""One current coverage rule per exact scope; preserve all deleted history."""

import json

from alembic import context, op
import sqlalchemy as sa

revision = "0009_coverage_rule_current_identity"
down_revision = "0008_coverage_writer_sources"
branch_labels = None
depends_on = None

GLOBAL = "uq_diagnosis_service_rule_current_global"
INSURER = "uq_diagnosis_service_rule_current_insurer"


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("Current rule identity migration requires online preflight.")
    connection = op.get_bind()
    try:
        _upgrade(connection)
    except sa.exc.SQLAlchemyError:
        raise RuntimeError(
            "Current rule identity migration failed; transaction must roll back."
        ) from None


def _upgrade(connection):
    if connection.dialect.name == "postgresql":
        if connection.get_isolation_level() != "READ COMMITTED":
            raise RuntimeError(
                "Current rule identity migration requires READ COMMITTED."
            )
        op.execute(
            "LOCK TABLE diagnosis_codes, service_codes, insurance_companies, "
            "diagnosis_service_rules IN SHARE ROW EXCLUSIVE MODE"
        )
    elif connection.dialect.name == "sqlite":
        # A SQLAlchemy transaction may not yet be a physical sqlite3 transaction.
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            # Reserve the writer even if the physical transaction began DEFERRED.
            # No row or revision value is changed.
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = version_num WHERE 0"
            )
    else:
        raise RuntimeError("Unsupported database for current rule identity migration.")

    conflicts = connection.execute(
        sa.text(
            "SELECT diagnosis_id, service_id, insurer_id, COUNT(*) AS current_count "
            "FROM diagnosis_service_rules WHERE is_deleted = false "
            "GROUP BY diagnosis_id, service_id, insurer_id HAVING COUNT(*) > 1 "
            "ORDER BY diagnosis_id, service_id, "
            "CASE WHEN insurer_id IS NULL THEN 0 ELSE 1 END, insurer_id"
        )
    ).all()
    if conflicts:
        details = [
            dict(
                diagnosis_id=row.diagnosis_id,
                service_id=row.service_id,
                scope="GLOBAL" if row.insurer_id is None else row.insurer_id,
                current_count=row.current_count,
            )
            for row in conflicts
        ]
        raise RuntimeError("Current coverage rule conflicts: " + json.dumps(details))

    if connection.dialect.name == "postgresql":
        # The required revision identifier exceeds Alembic's default VARCHAR(32).
        # Keep the widening in this transaction; failed DDL rolls it back too.
        version_column = next(
            column
            for column in sa.inspect(connection).get_columns("alembic_version")
            if column["name"] == "version_num"
        )
        length = getattr(version_column["type"], "length", None)
        if length is not None and length < len(revision):
            op.alter_column(
                "alembic_version",
                "version_num",
                existing_type=version_column["type"],
                type_=sa.String(len(revision)),
                existing_nullable=False,
            )

    for name, columns, predicate in (
        (
            GLOBAL,
            ["diagnosis_id", "service_id"],
            "insurer_id IS NULL AND is_deleted = false",
        ),
        (
            INSURER,
            ["diagnosis_id", "service_id", "insurer_id"],
            "insurer_id IS NOT NULL AND is_deleted = false",
        ),
    ):
        op.create_index(
            name,
            "diagnosis_service_rules",
            columns,
            unique=True,
            postgresql_where=sa.text(predicate),
            sqlite_where=sa.text(predicate),
        )


def downgrade():
    op.drop_index(INSURER, table_name="diagnosis_service_rules")
    op.drop_index(GLOBAL, table_name="diagnosis_service_rules")
