"""Test-only identities, generated independently of application credentials."""

from functools import lru_cache
import secrets
from auth import hash_password, create_access_token
from models import User


@lru_cache(maxsize=1)
def test_password_hash():
    return hash_password(secrets.token_urlsafe(32))


def provider_headers(session):
    user = User(
        username="test-provider", role="provider", password_hash=test_password_hash()
    )
    session.add(user)
    session.commit()
    return {"Authorization": "Bearer " + create_access_token(user)}
