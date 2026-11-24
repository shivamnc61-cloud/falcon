import asyncio

import aiosqlite
import pytest

from database import (
    _PostgresConnection,
    _postgres_url,
    _quote_postgres_reserved_identifiers,
)


class _MetadataCursor:
    def __init__(self):
        self._rows = []

    async def execute(self, query, parameters=None):
        if "table_constraints" in query:
            self._rows = [("guild_id",), ("role_id",)]
        else:
            self._rows = []

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class _MetadataConnection:
    def cursor(self):
        return _MetadataCursor()


def test_database_url_accepts_postgres_schemes_only() -> None:
    assert _postgres_url("postgresql://user:pass@localhost:5432/falcon")
    assert _postgres_url("postgres://user:pass@localhost/falcon")
    assert not _postgres_url("")
    with pytest.raises(ValueError, match="Unsupported DATABASE_URL scheme"):
        _postgres_url("mysql://localhost/falcon")


def test_postgres_reserved_identifier_quoting_leaves_text_literals_unchanged() -> None:
    assert _quote_postgres_reserved_identifiers(
        "SELECT left FROM users WHERE status='left'"
    ) == 'SELECT "left" FROM users WHERE status=\'left\''


def test_postgres_insert_translation_preserves_ignore_and_replace_semantics() -> None:
    async def verify() -> None:
        connection = _PostgresConnection(_MetadataConnection())
        ignore_sql, _ = await connection._translate_insert(
            "INSERT OR IGNORE INTO entries (guild_id, role_id) VALUES (?, ?)"
        )
        replace_sql, _ = await connection._translate_insert(
            "INSERT OR REPLACE INTO entries (guild_id, role_id, label) VALUES (?, ?, ?)"
        )
        assert ignore_sql.endswith("ON CONFLICT DO NOTHING")
        assert "ON CONFLICT (guild_id, role_id) DO UPDATE SET label=EXCLUDED.label" in replace_sql

    asyncio.run(verify())


def test_sqlite_database_fallback_remains_available(tmp_path) -> None:
    from database import connect_database

    async def verify() -> None:
        database = await connect_database("", ":memory:", project_dir=tmp_path)
        try:
            await database.execute("CREATE TABLE fallback (value INTEGER)")
            await database.execute("INSERT INTO fallback VALUES (?)", (1,))
            async with database.execute("SELECT value FROM fallback") as cursor:
                assert await cursor.fetchone() == (1,)
        finally:
            await database.close()

    asyncio.run(verify())
