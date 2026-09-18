"""Initialize local authentication settings without displaying or rotating secrets."""

from pathlib import Path
import secrets
from dotenv import dotenv_values, set_key


def initialize():
    path = Path(__file__).with_name(".env")
    values = dotenv_values(path)
    defaults = {
        "JWT_ISSUER": "nphies-claim-gate",
        "JWT_AUDIENCE": "nphies-api",
        "ACCESS_TOKEN_MINUTES": "15",
    }
    if not values.get("SECRET_KEY"):
        set_key(str(path), "SECRET_KEY", secrets.token_urlsafe(64))
    for name, value in defaults.items():
        if not values.get(name):
            set_key(str(path), name, value)
    print("Authentication settings initialized in .env; existing key preserved.")


if __name__ == "__main__":
    initialize()
