"""Test fixtures — SQLite in-memory DB + FastAPI TestClient."""
import os
from typing import Any, Generator

import pytest

# Force SQLite in-memory for tests BEFORE any app imports
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ACP_CACHE_PATH"] = "/tmp/test_acp_cache.json"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app

engine = create_engine(
    "sqlite:///file::memory:?cache=shared&uri=true",
    connect_args={"check_same_thread": False},
)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db() -> Generator[Session, None, None]:
    """Override DB dependency with test session."""
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="session", autouse=True)
def setup_db() -> Generator[None, None, None]:
    """Create and teardown all tables for the test session."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Provide a TestClient instance."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    """Provide a test DB session."""
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


def create_test_bounty(client: TestClient, **overrides: Any) -> dict[str, Any]:
    """Factory helper: create a bounty and return the full response JSON.

    Args:
        client: TestClient instance.
        **overrides: Override default bounty fields.

    Returns:
        The full response JSON dict.
    """
    payload = {
        "title": "Test Bounty",
        "description": "A test bounty for testing purposes",
        "budget": 100.0,
        "poster_name": "test-agent",
        "category": "digital",
    }
    payload.update(overrides)
    r = client.post("/api/v1/bounties/", json=payload)
    assert r.status_code in (200, 201), f"Failed to create bounty: {r.text}"
    return r.json()
