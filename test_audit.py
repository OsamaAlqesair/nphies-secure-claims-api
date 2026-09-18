"""Audit persistence, redaction and append-only enforcement."""

import pytest
from sqlalchemy import select, update
from models import AuditLog, DiagnosisServiceRule
from test_auth import client, credentials, identities, database, payload, login, headers


def logs(database):
    with database() as db:
        return db.scalars(select(AuditLog).order_by(AuditLog.id)).all()


def test_login_success_and_failure_persist_without_secrets(
    client, credentials, database, identities
):
    failed = client.post(
        "/auth/login", json={"username": "test-provider", "password": "wrong-password"}
    )
    token = login(client, credentials)
    rows = logs(database)
    assert [(r.action, r.outcome) for r in rows] == [
        ("auth.login", "failure"),
        ("auth.login", "success"),
    ]
    assert rows[0].request_id == failed.headers["X-Request-ID"]
    assert all(r.actor_user_id == identities["provider"] for r in rows)
    content = repr(
        [
            {c.name: getattr(row, c.name) for c in AuditLog.__table__.columns}
            for row in rows
        ]
    )
    assert (
        credentials[0] not in content
        and token not in content
        and "wrong-password" not in content
    )


def test_unknown_login_and_malformed_payload_recorded(client, database):
    client.post(
        "/auth/login", json={"username": "not-existing", "password": "private-value"}
    )
    client.post(
        "/auth/login",
        content='{"password":',
        headers={"Content-Type": "application/json"},
    )
    rows = logs(database)
    assert len(rows) == 2
    assert all(row.actor_user_id is None for row in rows)
    assert rows[1].reason == "invalid_request"


@pytest.mark.parametrize(
    ("changes", "reason", "status"),
    [
        ({}, "approved", 200),
        ({"net_payable": "350.01"}, "financial_invariant", 422),
        ({"diagnosis_code": "UNKNOWN"}, "unknown_diagnosis", 400),
    ],
)
def test_claim_results_audited(
    client, credentials, database, payload, identities, changes, reason, status
):
    token = login(client, credentials)
    payload.update(changes)
    response = client.post("/process-claim", json=payload, headers=headers(token))
    assert response.status_code == status
    row = logs(database)[-1]
    assert row.action == "claim.validation" and row.reason == reason
    assert row.http_status == status and row.actor_user_id == identities["provider"]
    assert row.request_id == response.headers["X-Request-ID"]
    assert row.outcome == ("success" if status == 200 else "failure")


def test_medical_rejection_audited(client, credentials, database, payload):
    with database() as db:
        db.add(
            DiagnosisServiceRule(
                diagnosis_id=1, service_id=1, insurer_id=1, is_covered=False
            )
        )
        db.commit()
    response = client.post(
        "/process-claim", json=payload, headers=headers(login(client, credentials))
    )
    assert response.status_code == 422
    assert logs(database)[-1].reason == "medical_necessity"


def test_denied_access_recorded(client, credentials, database, payload):
    client.post("/process-claim", json=payload)
    assert logs(database)[-1].action == "auth.authentication"
    client.get("/auth/users", headers=headers(login(client, credentials)))
    assert logs(database)[-1].action == "auth.authorization"


@pytest.mark.parametrize("action", ["update", "delete", "soft-delete", "bulk-update"])
def test_audit_is_append_only(client, credentials, database, action):
    login(client, credentials)
    with database() as db:
        row = db.scalar(select(AuditLog))
        with pytest.raises(ValueError, match="append-only"):
            if action == "update":
                row.reason = "changed"
                db.flush()
            elif action == "delete":
                db.delete(row)
                db.flush()
            elif action == "soft-delete":
                row.soft_delete()
            else:
                db.execute(update(AuditLog).values(reason="changed"))
        db.rollback()
    assert logs(database)[0].reason == "authenticated"
