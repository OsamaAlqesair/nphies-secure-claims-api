"""Run tests with generated credentials and repository dotenv loading blocked."""

import os
from pathlib import Path
import secrets
import sys


def main():
    os.environ.pop("TEST_DATABASE_URL", None)
    os.environ.update(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://nphies_test@127.0.0.1:1/nphies_test_disabled",
        PYTHON_DOTENV_DISABLED="1",
        TEST_POSTGRES_IMAGE="postgres:17",
    )
    import dotenv

    original = dotenv.dotenv_values
    repository_env = Path(__file__).with_name(".env").resolve()
    synthetic = dict(
        SECRET_KEY=secrets.token_urlsafe(64),
        JWT_ISSUER="nphies-test",
        JWT_AUDIENCE="nphies-test",
        ACCESS_TOKEN_MINUTES="15",
    )

    def isolated_values(path=None, *args, **kwargs):
        if path is not None and Path(path).resolve() == repository_env:
            return dict(synthetic)
        return original(path, *args, **kwargs)

    dotenv.dotenv_values = isolated_values
    import pytest

    return pytest.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
