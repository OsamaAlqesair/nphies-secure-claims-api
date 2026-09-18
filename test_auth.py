"""Real password/JWT checks against isolated database accounts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from auth import (
    AuthSettings,
    settings,
    hash_password,
    create_access_token,
    password_hasher,
)
from main import app, get_db
from models import User, DiagnosisServiceRule
from test_main import database, payload


@pytest.fixture(scope="module")
def credentials():
    password = secrets.token_urlsafe(32)
    return password, hash_password(password)


@pytest.fixture
def identities(database, credentials):
    with database() as db:
        users = [
            User(username="test-admin", role="admin", password_hash=credentials[1]),
            User(
                username="test-provider", role="provider", password_hash=credentials[1]
            ),
        ]
        db.add_all(users)
        db.add(
            DiagnosisServiceRule(
                diagnosis_id=1, service_id=1, insurer_id=None, is_covered=True
            )
        )
        db.commit()
        return {user.role: user.id for user in users}


@pytest.fixture
def client(database, identities):
    def override():
        with database() as db:
            yield db

    saved = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def login(client, credentials, role="provider"):
    response = client.post(
        "/auth/login", json={"username": "test-" + role, "password": credentials[0]}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    return response.json()["access_token"]


def headers(token):
    return {"Authorization": "Bearer " + token}


def denied(response, status=401):
    assert response.status_code == status
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == ("login" if status == 401 else "forbidden")
    assert response.headers["content-type"].startswith("application/fhir+json")
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization", [None, "Basic abc", "Bearer not-a-jwt", "Bearer"]
)
def test_claim_requires_bearer(client, payload, authorization):
    denied(
        client.post(
            "/process-claim",
            json=payload,
            headers={} if authorization is None else {"Authorization": authorization},
        )
    )


@pytest.mark.parametrize("role", ["admin", "provider"])
def test_login_and_claim_roles(client, credentials, payload, role):
    token = login(client, credentials, role)
    response = client.post("/process-claim", json=payload, headers=headers(token))
    assert response.status_code == 200
    assert response.json()["net_payable"] == "350.00"
    identity = client.get("/auth/me", headers=headers(token)).json()
    assert identity["role"] == role
    assert "password_hash" not in identity
    claims = jwt.decode(
        token,
        settings.secret_key,
        algorithms=["HS256"],
        issuer=settings.issuer,
        audience=settings.audience,
    )
    assert claims["exp"] - claims["iat"] == settings.lifetime_minutes * 60
    assert claims["token_use"] == "access"


def test_admin_only_user_list(client, credentials):
    denied(client.get("/auth/users", headers=headers(login(client, credentials))), 403)
    response = client.get(
        "/auth/users", headers=headers(login(client, credentials, "admin"))
    )
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert "password_hash" not in response.text and credentials[0] not in response.text


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "future",
        "wrong-audience",
        "wrong-issuer",
        "missing-exp",
        "wrong-use",
        "bad-sub",
        "wrong-version",
        "missing-jti",
        "wrong-algorithm",
        "wrong-signature",
    ],
)
def test_invalid_jwts_rejected(client, credentials, payload, change):
    token = login(client, credentials)
    claims = jwt.decode(
        token,
        settings.secret_key,
        algorithms=["HS256"],
        issuer=settings.issuer,
        audience=settings.audience,
    )
    algorithm = "HS256"
    key = settings.secret_key
    if change == "expired":
        claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
    elif change == "future":
        claims["nbf"] = int(datetime.now(timezone.utc).timestamp()) + 3600
    elif change == "wrong-audience":
        claims["aud"] = "another-service"
    elif change == "wrong-issuer":
        claims["iss"] = "another-issuer"
    elif change == "missing-exp":
        del claims["exp"]
    elif change == "wrong-use":
        claims["token_use"] = "refresh"
    elif change == "bad-sub":
        claims["sub"] = "-1"
    elif change == "wrong-version":
        claims["ver"] += 1
    elif change == "missing-jti":
        del claims["jti"]
    elif change == "wrong-algorithm":
        algorithm = "HS384"
    elif change == "wrong-signature":
        key = secrets.token_urlsafe(64)
    denied(
        client.post(
            "/process-claim",
            json=payload,
            headers=headers(jwt.encode(claims, key, algorithm=algorithm)),
        )
    )


@pytest.mark.parametrize("change", ["disabled", "deleted", "revoked"])
def test_account_state_invalidates_tokens(
    client, credentials, database, identities, change
):
    token = login(client, credentials)
    with database() as db:
        user = db.get(User, identities["provider"])
        if change == "disabled":
            user.is_active = False
        elif change == "deleted":
            user.soft_delete()
        else:
            user.token_version += 1
        db.commit()
    denied(client.get("/auth/me", headers=headers(token)))


def test_role_changes_apply_immediately(client, credentials, database, identities):
    token = login(client, credentials, "admin")
    with database() as db:
        db.get(User, identities["admin"]).role = "provider"
        db.commit()
    denied(client.get("/auth/users", headers=headers(token)), 403)


def test_lockout_and_recovery(client, credentials, database, identities):
    for _ in range(5):
        denied(
            client.post(
                "/auth/login",
                json={"username": "test-provider", "password": "incorrect-password"},
            )
        )
    denied(
        client.post(
            "/auth/login",
            json={"username": "test-provider", "password": credentials[0]},
        )
    )
    with database() as db:
        user = db.get(User, identities["provider"])
        assert user.failed_login_attempts == 5
        user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    login(client, credentials)
    with database() as db:
        user = db.get(User, identities["provider"])
        assert user.failed_login_attempts == 0 and user.locked_until is None


def test_unknown_and_disabled_users_have_same_failure(
    client, credentials, database, identities
):
    with database() as db:
        db.get(User, identities["provider"]).is_active = False
        db.commit()
    responses = [
        client.post("/auth/login", json={"username": name, "password": credentials[0]})
        for name in ["test-provider", "does-not-exist"]
    ]
    for response in responses:
        denied(response)
        assert credentials[0] not in response.text
    assert responses[0].json() == responses[1].json()


def test_cannot_choose_role_at_login(client, credentials):
    response = client.post(
        "/auth/login",
        json={"username": "test-provider", "password": credentials[0], "role": "admin"},
    )
    assert (
        response.status_code == 422
        and response.json()["resourceType"] == "OperationOutcome"
    )
    assert credentials[0] not in response.text


def test_password_storage_is_argon2id(credentials):
    assert credentials[1].startswith("$argon2id$")
    assert password_hasher.verify(credentials[0], credentials[1])
    assert not password_hasher.verify("incorrect-password", credentials[1])


@pytest.mark.parametrize("value", [None, "short"])
def test_config_fails_closed_without_strong_key(tmp_path, value):
    path = tmp_path / "config.env"
    if value is not None:
        path.write_text("SECRET_KEY=" + value)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        AuthSettings.from_file(path)


def test_openapi_requires_auth(client):
    document = client.get("/openapi.json").json()
    assert document["paths"]["/process-claim"]["post"]["security"] == [
        {"BearerAuth": []}
    ]
    assert document["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    assert not document["paths"]["/auth/login"]["post"].get("security")
    assert "HTTPValidationError" not in document["components"]["schemas"]
