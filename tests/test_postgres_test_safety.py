from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts.postgres_test_safety import (
    UnsafeTestDatabaseError,
    approve_test_database_environment,
    verify_connected_test_database,
)


SAFE_URL = (
    "postgresql://uwo_housing_test:uwo_housing_test_only@127.0.0.1:55601/"
    "uwo_housing_test"
)


def test_approves_explicit_loopback_test_database() -> None:
    target = approve_test_database_environment({"TEST_DATABASE_URL": SAFE_URL})

    assert target.database_name == "uwo_housing_test"
    assert target.hostname == "127.0.0.1"
    assert "uwo_housing_test_only" not in repr(target)
    assert SAFE_URL not in repr(target)


@pytest.mark.parametrize(
    "environment, expected_message",
    [
        ({}, "required"),
        ({"TEST_DATABASE_URL": ""}, "required"),
        (
            {"TEST_DATABASE_URL": "postgresql://user:password@localhost/housing"},
            "contain 'test'",
        ),
        (
            {
                "TEST_DATABASE_URL": (
                    "postgresql://user:password@localhost/uwo_production_test"
                )
            },
            "production or staging",
        ),
        (
            {
                "TEST_DATABASE_URL": (
                    "postgresql://user:password@localhost/uwo_staging_test"
                )
            },
            "production or staging",
        ),
        (
            {
                "TEST_DATABASE_URL": (
                    "postgresql://user:password@project.supabase.co/uwo_test"
                )
            },
            "Supabase",
        ),
        (
            {
                "TEST_DATABASE_URL": (
                    "postgresql://user:password@database.example/uwo_test"
                )
            },
            "loopback",
        ),
        (
            {"TEST_DATABASE_URL": "mysql://user:password@localhost/uwo_test"},
            "scheme",
        ),
        (
            {
                "TEST_DATABASE_URL": SAFE_URL,
                "DATABASE_URL": SAFE_URL,
            },
            "must not equal",
        ),
    ],
)
def test_rejects_unsafe_test_database_targets(
    environment: dict[str, str], expected_message: str
) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match=expected_message):
        approve_test_database_environment(environment)


def test_safety_errors_never_include_connection_credentials() -> None:
    unsafe_url = "postgresql://private_user:private_password@remote/uwo_test"

    with pytest.raises(UnsafeTestDatabaseError) as captured:
        approve_test_database_environment({"TEST_DATABASE_URL": unsafe_url})

    message = str(captured.value)
    assert "private_user" not in message
    assert "private_password" not in message
    assert unsafe_url not in message


@dataclass
class _FakeResult:
    row: tuple[str, str]

    def fetchone(self) -> tuple[str, str]:
        return self.row


@dataclass
class _FakeConnection:
    row: tuple[str, str]

    def execute(self, query: str) -> _FakeResult:
        assert query == "select current_database(), current_schema()"
        return _FakeResult(self.row)


def test_connected_database_and_public_schema_are_verified() -> None:
    target = approve_test_database_environment({"TEST_DATABASE_URL": SAFE_URL})
    verify_connected_test_database(
        _FakeConnection(("uwo_housing_test", "public")), target
    )

    with pytest.raises(UnsafeTestDatabaseError, match="does not match"):
        verify_connected_test_database(
            _FakeConnection(("another_test", "public")), target
        )
    with pytest.raises(UnsafeTestDatabaseError, match="does not match"):
        verify_connected_test_database(
            _FakeConnection(("uwo_housing_test", "private")), target
        )
