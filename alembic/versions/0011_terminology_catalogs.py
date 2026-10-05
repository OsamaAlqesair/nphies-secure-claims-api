"""Add versioned catalogs without reinterpreting legacy terminology."""

from alembic import context, op
import sqlalchemy as sa
from services.catalog_schema import tables, install_guards

revision = "0011_terminology_catalogs"
down_revision = "0010_claim_intake_history"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    catalog, entry = tables(sa.MetaData())
    catalog.create(connection)
    entry.create(connection)
    install_guards(
        connection, execute=op.execute if context.is_offline_mode() else None
    )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Catalog downgrade requires an online history check.")
    connection = op.get_bind()
    # Keep the emptiness check and table removal atomic with concurrent imports.
    # PostgreSQL retains these relation locks until the migration transaction ends.
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "LOCK TABLE terminology_catalogs, terminology_catalog_entries "
            "IN ACCESS EXCLUSIVE MODE"
        )
    elif connection.dialect.name == "sqlite":
        # A no-row write starts SQLite's write transaction without changing data.
        connection.exec_driver_sql(
            "UPDATE terminology_catalogs SET id = id WHERE 1 = 0"
        )
    if connection.exec_driver_sql("SELECT count(*) FROM terminology_catalogs").scalar():
        raise RuntimeError(
            "Refusing downgrade: persisted catalog provenance/history exists."
        )
    op.drop_table("terminology_catalog_entries")
    op.drop_table("terminology_catalogs")
    if connection.dialect.name == "postgresql":
        for name in (
            "guard_terminology_catalog",
            "guard_terminology_entry",
            "guard_terminology_truncate",
        ):
            connection.exec_driver_sql(f"DROP FUNCTION {name}()")
