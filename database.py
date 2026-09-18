"""PostgreSQL configuration; SQLite is allowed only as an explicit test override."""

import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

load_dotenv(Path(__file__).with_name(".env"))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://nphies@127.0.0.1:5432/nphies"
)
url = make_url(DATABASE_URL)
if url.drivername in {"postgres", "postgresql"}:
    url = url.set(drivername="postgresql+psycopg")
if url.get_backend_name() == "sqlite" and os.environ.get("APP_ENV") != "test":
    raise RuntimeError("SQLite is test-only. Configure DATABASE_URL for PostgreSQL.")
engine = create_engine(
    url,
    pool_pre_ping=True,
    connect_args=(
        {"check_same_thread": False}
        if url.get_backend_name() == "sqlite"
        else {"connect_timeout": 5}
    ),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    with SessionLocal() as session:
        yield session
