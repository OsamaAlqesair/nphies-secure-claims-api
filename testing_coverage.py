"""Test-only historical setup with exact, single-use Core authorization.

No ORM listener is removed or authorized. Each helper issues one exact payload
on a fixture-owned Connection, leaving history append-only protections enabled.
Never imported by application modules.
"""

from contextlib import contextmanager
import re
import subprocess
from weakref import WeakKeyDictionary

import sqlalchemy as sa
from models import DiagnosisServiceRule
from services.coverage_write_guards import (
    register_coverage_engine,
    _historical_execution,
)

_owners = WeakKeyDictionary()
_containers = WeakKeyDictionary()


@contextmanager
def owned_sqlite(engine):
    if engine.dialect.name != "sqlite":
        raise ValueError("Fixture-owned SQLite required.")
    register_coverage_engine(engine)
    with engine.connect() as connection:
        databases = connection.exec_driver_sql("PRAGMA database_list").all()
    if any(name not in ("main", "temp") or filename for _, name, filename in databases):
        # Current exceptional fixtures use memory only. Do not trust a caller's
        # arbitrary filename as proof that a persistent database is disposable.
        raise ValueError("Fixture-owned in-memory SQLite required.")
    if engine in _owners:
        raise ValueError("Disposable engine already registered.")
    _owners[engine] = ("sqlite", None)
    try:
        yield
    finally:
        _owners.pop(engine, None)


@contextmanager
def owned_postgres_container(engine, name, container_id):
    if not re.fullmatch(r"nphies-identity-test-[0-9a-f]{32}", name) or not re.fullmatch(
        r"[0-9a-f]{64}", container_id
    ):
        raise ValueError("Fixture-created container required.")
    actual = subprocess.check_output(
        [
            "docker",
            "--host",
            "npipe:////./pipe/docker_engine",
            "inspect",
            "--format",
            "{{.Id}}",
            name,
        ],
        text=True,
        timeout=10,
    ).strip()
    if actual != container_id or engine.dialect.name != "postgresql":
        raise ValueError("Disposable container ownership mismatch.")
    container_identity = subprocess.check_output(
        [
            "docker",
            "--host",
            "npipe:////./pipe/docker_engine",
            "exec",
            name,
            "psql",
            "-U",
            "postgres",
            "-d",
            "nphies_identity_test",
            "-tAc",
            "SELECT system_identifier FROM pg_control_system()",
        ],
        text=True,
        timeout=10,
    ).strip()
    with engine.connect() as connection:
        identity = connection.scalar(
            sa.text("SELECT system_identifier FROM pg_control_system()")
        )
        if str(identity) != container_identity:
            raise ValueError("Engine does not belong to the fixture container.")
        if (
            connection.scalar(sa.text("SELECT current_database()"))
            != "nphies_identity_test"
        ):
            raise ValueError("Disposable database mismatch.")
    _containers[engine] = identity
    try:
        yield
    finally:
        _containers.pop(engine, None)


@contextmanager
def owned_postgres_schema(engine, root, schema):
    if root not in _containers or not re.fullmatch(
        r"(?:history|writer|guard)_test_[0-9a-f]{32}", schema
    ):
        raise ValueError("Fixture-owned PostgreSQL schema required.")
    register_coverage_engine(engine)
    with engine.connect() as connection:
        if (
            connection.scalar(
                sa.text("SELECT system_identifier FROM pg_control_system()")
            )
            != _containers[root]
        ):
            raise ValueError("Disposable server ownership mismatch.")
        if connection.scalar(sa.text("SELECT current_schema()")) != schema:
            raise ValueError("Disposable schema ownership mismatch.")
    _owners[engine] = ("postgresql", schema)
    try:
        yield
    finally:
        _owners.pop(engine, None)


def _connection(db):
    connection = db.connection()
    _verify_connection(connection)
    return connection


def _verify_connection(connection):
    owner = _owners.get(connection.engine)
    if owner is None:
        raise ValueError("Unregistered disposable database; rule setup refused.")
    if connection.get_execution_options().get("schema_translate_map"):
        raise ValueError("Fixture connection schema translation refused.")
    if owner[0] == "postgresql":
        if connection.scalar(sa.text("SELECT current_schema()")) != owner[1]:
            raise ValueError("Fixture connection changed its owned schema.")
    else:
        databases = connection.exec_driver_sql("PRAGMA database_list").all()
        if any(
            name not in ("main", "temp") or filename for _, name, filename in databases
        ):
            raise ValueError("Fixture connection has an unowned SQLite database.")


def historical_row(connection, **payload):
    """One exact Core INSERT for a registered malformed/historical fixture."""
    _verify_connection(connection)
    if not payload or not set(payload).issubset(
        DiagnosisServiceRule.__table__.columns.keys()
    ):
        raise ValueError("Exact fixture rule payload required.")
    statement = DiagnosisServiceRule.__table__.insert()
    with _historical_execution(connection, statement, payload):
        return connection.execute(statement, payload)


def historical_rules(db, *rules):
    """Insert exact transient fixture instances without fabricating past history."""
    connection = _connection(db)
    for rule in rules:
        state = sa.inspect(rule)
        if not isinstance(rule, DiagnosisServiceRule) or not state.transient:
            raise ValueError("Exact transient fixture rule required.")
        if any(
            state.attrs[key].history.has_changes()
            for key in ("diagnosis", "service", "insurer")
        ):
            raise ValueError("Historical fixture setup requires explicit mapping IDs.")
        payload = {
            column.key: state.dict[column.key]
            for column in DiagnosisServiceRule.__table__.columns
            if column.key in state.dict
        }
        result = historical_row(connection, **payload)
        rule.id = result.inserted_primary_key[0]


def historical_change(db, rule, **values):
    """Change an exact baseline row only when its test requires empty history."""
    connection = _connection(db)
    if not isinstance(rule, DiagnosisServiceRule) or sa.inspect(rule).session is not db:
        raise ValueError("Exact fixture Session and rule required.")
    if not values or not set(values).issubset({"is_covered", "is_deleted"}):
        raise ValueError("Unsupported historical setup operation.")
    statement = DiagnosisServiceRule.__table__.update().where(
        DiagnosisServiceRule.id == sa.bindparam("diagnosis_service_rules_id")
    )
    payload = {**values, "diagnosis_service_rules_id": rule.id}
    with _historical_execution(connection, statement, payload):
        connection.execute(statement, payload)
    db.expire(rule)


def audited_rule(db, rule):
    """Ordinary valid fixture setup follows the same services as applications."""
    from services.coverage_mutations import (
        create_rule,
        import_rule,
        soft_delete_rule,
        MutationContext,
    )

    if not isinstance(rule, DiagnosisServiceRule) or not sa.inspect(rule).transient:
        raise ValueError("Transient fixture rule required.")
    if not db.in_transaction():
        db.begin()
    db.flush()  # Persist unrelated fixture mappings before service preflight.
    context = MutationContext(
        reason="Create synthetic test coverage", source="coverage_mutations.py"
    )
    if rule.id is not None:
        return import_rule(
            db,
            rule.id,
            rule.diagnosis_id,
            rule.service_id,
            bool(rule.is_covered),
            insurer_id=rule.insurer_id,
            is_deleted=bool(rule.is_deleted),
            context=context,
        )
    created = create_rule(
        db,
        rule.diagnosis_id,
        rule.service_id,
        bool(rule.is_covered),
        insurer_id=rule.insurer_id,
        context=context,
    )
    if rule.is_deleted:
        soft_delete_rule(db, created.id, context=context)
    return created
