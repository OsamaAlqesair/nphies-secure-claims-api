"""Phase 1D persistence contract. Freeze v1 for migration 0011 compatibility."""

import sqlalchemy as sa


def tables(metadata):
    catalog = sa.Table(
        "terminology_catalogs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("family", sa.String, nullable=False),
        sa.Column("code_system_url", sa.String, nullable=False),
        sa.Column("edition", sa.String, nullable=False),
        sa.Column("source_filename", sa.String, nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("extraction_context", sa.String, nullable=False),
        sa.Column("reconstruction_label", sa.String, nullable=False),
        sa.Column("parser_version", sa.String, nullable=False),
        sa.Column("artifact_schema", sa.String, nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("import_sha256", sa.String(64)),
        sa.Column("expected_count", sa.Integer, nullable=False),
        sa.Column("imported_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="IMPORTING"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "family",
            "edition",
            "source_sha256",
            "parser_version",
            "artifact_schema",
            name="uq_catalog_source_identity",
        ),
        sa.CheckConstraint(
            "state IN ('IMPORTING','VALIDATED','ACTIVE','FAILED','SUPERSEDED')",
            name="ck_catalog_state",
        ),
        sa.CheckConstraint(
            "expected_count > 0 AND imported_count >= 0 AND imported_count <= expected_count",
            name="ck_catalog_counts",
        ),
        sa.CheckConstraint(
            "state NOT IN ('VALIDATED','ACTIVE','SUPERSEDED') OR (imported_count = expected_count AND import_sha256 IS NOT NULL AND import_sha256 = content_sha256 AND validated_at IS NOT NULL)",
            name="ck_catalog_complete",
        ),
        sa.CheckConstraint(
            "state NOT IN ('ACTIVE','SUPERSEDED') OR activated_at IS NOT NULL",
            name="ck_catalog_activation",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64 AND length(artifact_sha256) = 64 AND length(content_sha256) = 64",
            name="ck_catalog_hash_lengths",
        ),
    )
    sa.Index(
        "uq_catalog_active_family",
        catalog.c.family,
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
        sqlite_where=sa.text("state = 'ACTIVE'"),
    )
    entry = sa.Table(
        "terminology_catalog_entries",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "catalog_id",
            sa.Integer,
            sa.ForeignKey("terminology_catalogs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("code", sa.String, nullable=False),
        sa.Column("display", sa.String, nullable=False),
        sa.Column("display_confidence", sa.String, nullable=False),
        sa.Column("source_line", sa.Integer, nullable=False),
        sa.Column("classification", sa.String, nullable=False),
        sa.UniqueConstraint("catalog_id", "code", name="uq_catalog_entry_identity"),
        sa.CheckConstraint(
            "classification = 'leaf_candidate'", name="ck_catalog_entry_classification"
        ),
        sa.CheckConstraint(
            "display_confidence IN ('clean_single_line','flagged_ambiguous')",
            name="ck_catalog_entry_display",
        ),
        sa.CheckConstraint(
            "source_line > 0 AND length(trim(code)) > 0", name="ck_catalog_entry_source"
        ),
    )
    return catalog, entry


def install_guards(connection, *, execute=None):
    """Database-authoritative sealing, count validation and transition guards.

    Entries lock their parent before DML on PostgreSQL, serializing seal versus
    writes. SQLite serializes writers. Owners disabling triggers are out of scope.
    """
    execute = execute or connection.exec_driver_sql
    immutable = (
        "id family code_system_url edition source_filename source_sha256 extraction_context "
        "reconstruction_label parser_version artifact_schema artifact_sha256 content_sha256 expected_count created_at"
    ).split()
    evidence = "imported_count import_sha256 validated_at".split()
    if connection.dialect.name == "postgresql":
        changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in immutable)
        evidence_changed = " OR ".join(
            f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in evidence
        )
        execute(f"""
        CREATE FUNCTION guard_terminology_catalog() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Catalog history cannot be deleted'; END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.state <> 'IMPORTING' OR NEW.imported_count <> 0 OR NEW.import_sha256 IS NOT NULL
              OR NEW.validated_at IS NOT NULL OR NEW.activated_at IS NOT NULL THEN
              RAISE EXCEPTION 'Catalog must start importing'; END IF;
            RETURN NEW;
          END IF;
          IF {changed} THEN RAISE EXCEPTION 'Catalog provenance is immutable'; END IF;
          IF NOT ((OLD.state = 'IMPORTING' AND NEW.state IN ('VALIDATED','FAILED'))
             OR (OLD.state = 'VALIDATED' AND NEW.state = 'ACTIVE')
             OR (OLD.state = 'ACTIVE' AND NEW.state = 'SUPERSEDED')) THEN
             RAISE EXCEPTION 'Invalid catalog transition'; END IF;
          IF OLD.state <> 'IMPORTING' AND ({evidence_changed}) THEN
             RAISE EXCEPTION 'Catalog evidence is immutable'; END IF;
          IF NOT (OLD.state = 'VALIDATED' AND NEW.state = 'ACTIVE') AND
             NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
             RAISE EXCEPTION 'Catalog activation evidence is immutable'; END IF;
          IF NEW.state = 'VALIDATED' AND (SELECT count(*) FROM terminology_catalog_entries WHERE catalog_id = OLD.id) <> NEW.expected_count THEN
             RAISE EXCEPTION 'Catalog entry count mismatch'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER terminology_catalog_guard BEFORE INSERT OR UPDATE OR DELETE ON terminology_catalogs
        FOR EACH ROW EXECUTE FUNCTION guard_terminology_catalog();
        CREATE FUNCTION guard_terminology_entry() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_state text;
        BEGIN
          IF TG_OP = 'UPDATE' AND NEW.catalog_id <> OLD.catalog_id THEN
             RAISE EXCEPTION 'Catalog entry cannot move'; END IF;
          SELECT state INTO parent_state FROM terminology_catalogs
            WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.catalog_id ELSE NEW.catalog_id END FOR UPDATE;
          IF parent_state IS DISTINCT FROM 'IMPORTING' THEN RAISE EXCEPTION 'Catalog entries are sealed'; END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER terminology_entry_guard BEFORE INSERT OR UPDATE OR DELETE ON terminology_catalog_entries
        FOR EACH ROW EXECUTE FUNCTION guard_terminology_entry();
        CREATE FUNCTION guard_terminology_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Catalog history cannot be truncated'; END $$;
        CREATE TRIGGER terminology_catalog_truncate BEFORE TRUNCATE ON terminology_catalogs
        EXECUTE FUNCTION guard_terminology_truncate();
        CREATE TRIGGER terminology_entry_truncate BEFORE TRUNCATE ON terminology_catalog_entries
        EXECUTE FUNCTION guard_terminology_truncate();
        """)
    else:
        changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in immutable)
        evidence_changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in evidence)
        statements = [
            """CREATE TRIGGER terminology_catalog_insert BEFORE INSERT ON terminology_catalogs
            WHEN NEW.state <> 'IMPORTING' OR NEW.imported_count <> 0 OR NEW.import_sha256 IS NOT NULL
            OR NEW.validated_at IS NOT NULL OR NEW.activated_at IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'Catalog must start importing'); END""",
            """CREATE TRIGGER terminology_catalog_delete BEFORE DELETE ON terminology_catalogs
            BEGIN SELECT RAISE(ABORT, 'Catalog history cannot be deleted'); END""",
            f"""CREATE TRIGGER terminology_catalog_update BEFORE UPDATE ON terminology_catalogs BEGIN
            SELECT RAISE(ABORT, 'Catalog provenance is immutable') WHERE {changed};
            SELECT RAISE(ABORT, 'Invalid catalog transition') WHERE NOT
             ((OLD.state = 'IMPORTING' AND NEW.state IN ('VALIDATED','FAILED')) OR
              (OLD.state = 'VALIDATED' AND NEW.state = 'ACTIVE') OR
              (OLD.state = 'ACTIVE' AND NEW.state = 'SUPERSEDED'));
            SELECT RAISE(ABORT, 'Catalog evidence is immutable') WHERE OLD.state <> 'IMPORTING' AND ({evidence_changed});
            SELECT RAISE(ABORT, 'Catalog activation evidence is immutable') WHERE
              NOT (OLD.state = 'VALIDATED' AND NEW.state = 'ACTIVE') AND NEW.activated_at IS NOT OLD.activated_at;
            SELECT RAISE(ABORT, 'Catalog entry count mismatch') WHERE NEW.state = 'VALIDATED' AND
             (SELECT count(*) FROM terminology_catalog_entries WHERE catalog_id = OLD.id) <> NEW.expected_count;
            END""",
        ]
        for operation in ("INSERT", "UPDATE", "DELETE"):
            ref = "OLD" if operation == "DELETE" else "NEW"
            condition = f"(SELECT state FROM terminology_catalogs WHERE id = {ref}.catalog_id) IS NOT 'IMPORTING'"
            if operation == "UPDATE":
                condition += " OR NEW.catalog_id <> OLD.catalog_id"
            statements.append(
                f"CREATE TRIGGER terminology_entry_{operation.lower()} BEFORE {operation} ON terminology_catalog_entries WHEN {condition} BEGIN SELECT RAISE(ABORT, 'Catalog entries are sealed'); END"
            )
        for statement in statements:
            execute(statement)
