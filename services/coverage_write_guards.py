"""Private ORM coverage authorization; no general Core/driver SQL enforcement."""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from weakref import WeakKeyDictionary, WeakSet, ref

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, object_session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import Insert, Update, Delete
from sqlalchemy.sql.selectable import TableClause


class CoverageMutationError(ValueError):
    """A controlled diagnostic without database parameters or exception text."""

    def __init__(self, message, *, code="rule_mutation_failed"):
        super().__init__(message)
        self.code = code


_entries = WeakKeyDictionary()
_permits = WeakKeyDictionary()
_obligations = WeakKeyDictionary()
_watched = WeakSet()
_columns = (
    "id",
    "diagnosis_id",
    "service_id",
    "insurer_id",
    "is_covered",
    "is_deleted",
    "created_at",
    "updated_at",
)
_relationships = ("diagnosis", "service", "insurer")
_history_columns = (
    "action",
    "diagnosis_id",
    "service_id",
    "insurer_id",
    "diagnosis_code",
    "service_code",
    "insurer_name",
    "old_is_covered",
    "new_is_covered",
    "old_is_deleted",
    "new_is_deleted",
    "actor_user_id",
    "source",
    "reason",
)


def _reject():
    raise CoverageMutationError(
        "Coverage rule writes require audited mutation services.",
        code="rule_write_unauthorized",
    )


def _rollback_required():
    raise CoverageMutationError(
        "Coverage history is incomplete; roll back the transaction.",
        code="rule_history_incomplete",
    )


def _snapshot(obj, names):
    state = inspect(obj)
    # Read instrumentation without loading expired attributes or autoflushing.
    return {name: state.dict.get(name) for name in names}


def _changed(obj):
    return {
        name
        for name in _columns + _relationships
        if inspect(obj).attrs[name].history.has_changes()
    }


def _check_connection(connection):
    if _obligations.get(connection):
        _rollback_required()


def _on_commit(connection):
    _check_connection(connection)


def _on_rollback(connection):
    _obligations.pop(connection, None)


def _on_release_savepoint(connection, name, context):
    # Failed operations require the outer rollback, even inside caller savepoints.
    _check_connection(connection)


def _watch(connection):
    if connection not in _watched:
        event.listen(connection, "commit", _on_commit)
        event.listen(connection, "rollback", _on_rollback)
        event.listen(connection, "release_savepoint", _on_release_savepoint)
        _watched.add(connection)


@event.listens_for(Session, "before_commit")
def _before_commit(session):
    if session in _entries:
        _rollback_required()
    # Do not enlist a new connection for unrelated/logical Session transactions.
    for obligations in list(_obligations.values()):
        if any(permit.session is session for permit in obligations):
            _rollback_required()


def _audited_service(function):
    @wraps(function)
    def wrapped(session, *args, **kwargs):
        if session in _entries:
            raise CoverageMutationError(
                "Reentrant coverage mutation refused.", code="rule_mutation_reentrant"
            )
        if not session.in_transaction():
            raise CoverageMutationError(
                "An active caller-owned transaction is required."
            )
        _check_connection(session.connection())
        _entries[session] = (
            session.get_transaction(),
            session.get_nested_transaction(),
        )
        try:
            return function(session, *args, **kwargs)
        finally:
            _entries.pop(session, None)

    return wrapped


@dataclass(eq=False)
class _Permit:
    session: Session
    rule: object
    operation: str
    history: object
    transaction: tuple
    before: dict
    after: dict
    fields: set
    history_state: dict
    connection_ref: object
    root_ref: object
    rule_written: bool = False
    history_written: bool = False

    def check_transaction(self):
        connection = self.connection_ref()
        if (
            _entries.get(self.session) != self.transaction
            or (self.session.get_transaction(), self.session.get_nested_transaction())
            != self.transaction
            or connection is None
            or connection.get_transaction() is not self.root_ref()
            or not self.session.is_active
        ):
            _reject()


@contextmanager
def _authorize(session, rule, operation, history):
    if session not in _entries or session in _permits:
        _reject()
    state = inspect(rule)
    if _changed(rule) and not state.transient:
        _reject()
    if not inspect(history).transient:
        _reject()
    before = _snapshot(rule, _columns)
    after = dict(before)
    fields = set()
    if operation in ("CREATE", "IMPORT"):
        if not state.transient or history.action != "CREATE":
            _reject()
        if history.old_is_covered is not None or history.old_is_deleted is not None:
            _reject()
        if operation == "CREATE" and (rule.id is not None or rule.is_deleted):
            _reject()
        fields = set(_columns)
    elif operation == "UPDATE":
        fields = {"is_covered"}
        after["is_covered"] = history.new_is_covered
    elif operation in ("SOFT_DELETE", "RESTORE"):
        fields = {"is_deleted"}
        after["is_deleted"] = operation == "SOFT_DELETE"
    else:
        _reject()
    if operation not in ("CREATE", "IMPORT"):
        if not state.persistent or history.action != operation:
            _reject()
        if (
            history.old_is_covered != before["is_covered"]
            or history.old_is_deleted != before["is_deleted"]
        ):
            _reject()
        if operation == "UPDATE" and before["is_deleted"]:
            _reject()
        if (
            operation in ("SOFT_DELETE", "RESTORE")
            and before["is_deleted"] == after["is_deleted"]
        ):
            _reject()
    for name in ("diagnosis_id", "service_id", "insurer_id"):
        if getattr(history, name) != after[name]:
            _reject()
    if (
        history.new_is_covered != after["is_covered"]
        or history.new_is_deleted != after["is_deleted"]
    ):
        _reject()
    connection = session.connection()
    _watch(connection)
    permit = _Permit(
        session,
        rule,
        operation,
        history,
        _entries[session],
        before,
        after,
        fields,
        _snapshot(history, _history_columns),
        ref(connection),
        ref(connection.get_transaction()),
    )
    _permits[session] = permit
    obligations = _obligations.setdefault(connection, set())
    obligations.add(permit)
    try:
        yield
        permit.check_transaction()
        if not permit.rule_written or not permit.history_written:
            _rollback_required()
        if (
            _snapshot(history, _history_columns) != permit.history_state
            or history.rule_id != rule.id
        ):
            _rollback_required()
        if (
            _changed(rule)
            or session.is_modified(history)
            or not inspect(history).persistent
        ):
            _rollback_required()
        obligations.discard(permit)
        if not obligations:
            _obligations.pop(connection, None)
    except BaseException:
        # Even a non-SQL exception after the first successful flush leaves an
        # obligation behind. Session close must not erase a joined obligation.
        if not permit.rule_written:
            obligations.discard(permit)
            if not obligations:
                _obligations.pop(connection, None)
        raise
    finally:
        _permits.pop(session, None)


def _validate_rule(session, rule, *, inserting=False):
    changes = _changed(rule)
    if not inserting and not changes:
        return
    permit = _permits.get(session)
    if permit is None or permit.rule is not rule:
        _reject()
    permit.check_transaction()
    if inserting != (
        permit.operation in ("CREATE", "IMPORT") and not permit.rule_written
    ):
        _reject()
    if changes.intersection(_relationships) or not changes.issubset(permit.fields):
        _reject()
    if _snapshot(rule, _columns) != permit.after:
        _reject()
    if not inserting:
        for name in changes:
            history = inspect(rule).attrs[name].history
            if not history.deleted or history.deleted[0] != permit.before[name]:
                _reject()


def _before_flush(session):
    from models import DiagnosisServiceRule

    for rule in list(session.deleted):
        if isinstance(rule, DiagnosisServiceRule):
            _reject()
    for rule in list(session.new):
        if isinstance(rule, DiagnosisServiceRule):
            _validate_rule(session, rule, inserting=True)
    for rule in list(session.dirty):
        if isinstance(rule, DiagnosisServiceRule):
            _validate_rule(session, rule)
    permit = _permits.get(session)
    if permit and permit.history in session.new:
        if (
            _snapshot(permit.history, _history_columns) != permit.history_state
            or permit.history.rule_id != permit.rule.id
        ):
            _rollback_required()


def _orm_execute(state):
    from models import DiagnosisServiceRule

    mapper = state.bind_mapper
    if (
        (state.is_insert or state.is_update or state.is_delete)
        and mapper is not None
        and mapper.class_ is DiagnosisServiceRule
    ):
        _reject()
    for node in visitors.iterate(state.statement):
        if isinstance(node, (Insert, Update, Delete)):
            # Core DML may target an alias rather than the Table itself.
            if any(
                isinstance(target, TableClause)
                and target.name == "diagnosis_service_rules"
                for target in visitors.iterate(node.table)
            ):
                _reject()


def _before_rule_insert(mapper, connection, rule):
    session = object_session(rule)
    _validate_rule(session, rule, inserting=True)
    if _permits[session].connection_ref() is not connection:
        _reject()


def _before_rule_update(mapper, connection, rule):
    session = object_session(rule)
    _validate_rule(session, rule)
    if _changed(rule) and _permits[session].connection_ref() is not connection:
        _reject()


def _before_rule_delete(mapper, connection, rule):
    _reject()


def _after_rule_write(mapper, connection, rule):
    permit = _permits.get(object_session(rule))
    if permit is not None and permit.rule is rule:
        permit.rule_written = True
        # Generated IDs/timestamps are legitimate results, not application edits.
        for name in ("id", "created_at", "updated_at"):
            permit.after[name] = inspect(rule).dict.get(name)


def _after_history_insert(mapper, connection, history):
    permit = _permits.get(object_session(history))
    if permit is not None and permit.history is history:
        permit.history_written = True


def _before_history_insert(mapper, connection, history):
    permit = _permits.get(object_session(history))
    if permit is not None and permit.history is history:
        permit.check_transaction()
        if connection is not permit.connection_ref():
            _reject()
        if (
            _snapshot(history, _history_columns) != permit.history_state
            or history.rule_id != permit.rule.id
        ):
            _rollback_required()
