"""Keep reviewed synthetic manifests scoped to their individual test."""

import pytest


@pytest.fixture(autouse=True)
def isolated_catalog_manifests(monkeypatch):
    from services import catalog_import

    monkeypatch.setattr(
        catalog_import, "REVIEWED_MANIFESTS", (catalog_import.TENTH_EDITION,)
    )
