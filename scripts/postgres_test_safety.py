"""Safety checks shared by disposable PostgreSQL integration-test tooling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urlsplit


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
BLOCKED_DATABASE_PARTS = frozenset({"prod", "production", "stage", "staging"})


class UnsafeTestDatabaseError(ValueError):
    """The configured integration-test database is missing or unsafe."""


@dataclass(frozen=True)
class TestDatabaseTarget:
    """An approved local test database target without printable credentials."""

    url: str = field(repr=False)
    database_name: str
    hostname: str


def _database_name_from_url(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL is malformed") from error
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL must use the postgres or postgresql scheme"
        )
    hostname = (parsed.hostname or "").casefold()
    if not hostname:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL must include a hostname")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL contains an invalid port")
    raw_path = parsed.path.lstrip("/")
    if not raw_path or "/" in raw_path:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL must name exactly one test database"
        )
    database_name = unquote(raw_path)
    if not database_name:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL has no database name")
    return database_name, hostname


def approve_test_database_environment(
    environment: Mapping[str, str],
) -> TestDatabaseTarget:
    """Approve only an explicit, loopback ``TEST_DATABASE_URL``.

    ``DATABASE_URL`` is read solely to prevent accidental equality. It is never
    used as a fallback and neither value is logged or included in exceptions.
    """

    test_url = environment.get("TEST_DATABASE_URL", "").strip()
    if not test_url:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL is required")
    database_url = environment.get("DATABASE_URL", "").strip()
    if database_url and test_url == database_url:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL must not equal DATABASE_URL"
        )

    database_name, hostname = _database_name_from_url(test_url)
    normalized_name = database_name.casefold()
    if "test" not in normalized_name:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL database name must contain 'test'"
        )
    name_parts = {
        part for part in normalized_name.replace("-", "_").split("_") if part
    }
    if (
        name_parts & BLOCKED_DATABASE_PARTS
        or "production" in normalized_name
        or "staging" in normalized_name
    ):
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL names a production or staging database"
        )
    if "supabase" in hostname:
        raise UnsafeTestDatabaseError(
            "Supabase hosts are not allowed for local PostgreSQL integration tests"
        )
    if hostname not in LOOPBACK_HOSTS:
        raise UnsafeTestDatabaseError(
            "PostgreSQL integration tests require a loopback database host"
        )
    return TestDatabaseTarget(test_url, database_name, hostname)


def verify_connected_test_database(
    connection: Any, target: TestDatabaseTarget
) -> None:
    """Verify the connected database and schema before any destructive cleanup."""

    row: Optional[tuple[str, str]] = connection.execute(
        "select current_database(), current_schema()"
    ).fetchone()
    if row is None or row[0] != target.database_name or row[1] != "public":
        raise UnsafeTestDatabaseError(
            "Connected database or schema does not match the approved test target"
        )
