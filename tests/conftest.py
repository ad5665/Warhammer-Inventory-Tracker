from __future__ import annotations

import uuid

import pytest

import app.db as db
import app.storage as storage


@pytest.fixture(autouse=True)
def isolated_postgres_schema(monkeypatch):
    schema = f"test_{uuid.uuid4().hex}"
    monkeypatch.setattr(db, "DB_SCHEMA", schema)
    monkeypatch.setattr(storage, "STORAGE_BACKEND", "memory")
    storage.clear_memory_storage()
    try:
        yield
    finally:
        storage.clear_memory_storage()
        db.drop_schema(schema)
