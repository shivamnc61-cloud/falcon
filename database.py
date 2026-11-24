import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite


_PRAGMA_TABLE_INFO = re.compile(
    r"^\s*PRAGMA\s+table_info\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;?\s*$",
    re.IGNORECASE,
)
_INSERT_TABLE_AND_COLUMNS = re.compile(
    r"^\s*INSERT\s+(?:OR\s+(?:IGNORE|REPLACE)\s+)?INTO\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_INSERT_PREFIX = re.compile(r"^\s*INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+", re.IGNORECASE)
_AUTOINCREMENT_COLUMN = re.compile(
    r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_SQL_QUOTED_SEGMENT = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")")


def _postgres_url(database_url: str) -> bool:
    scheme = urlsplit(database_url).scheme.casefold()
    if scheme in {"postgres", "postgresql"}:
        return True
    if scheme:
        raise ValueError(f"Unsupported DATABASE_URL scheme: {scheme}")
    return False


def _quote_postgres_reserved_identifiers(sql: str) -> str:
    parts = _SQL_QUOTED_SEGMENT.split(sql)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\bleft\b", '"left"', parts[index], flags=re.IGNORECASE)
    return "".join(parts)


async def connect_database(
    database_url: str,
    database_path: str,
    *,
    project_dir: str | Path | None = None,
):
    if database_url:
        if not _postgres_url(database_url):
            raise ValueError("DATABASE_URL must use the postgresql:// scheme.")
        return await _PostgresConnection.connect(database_url)

    if database_path == ":memory:":
        return await aiosqlite.connect(database_path)

    project_root = Path(project_dir or Path(__file__).resolve().parent).resolve()
    requested_path = Path(database_path).expanduser()
    if not requested_path.is_absolute():
        requested_path = project_root / requested_path
    requested_path = requested_path.resolve()

    legacy_path = (project_root / "falcon_data.db").resolve()
    persistent_path = (project_root / "data" / "falcon_data.db").resolve()
    if requested_path in {legacy_path, persistent_path}:
        requested_path = persistent_path
        requested_path.parent.mkdir(parents=True, exist_ok=True)
        if not requested_path.exists() and legacy_path.exists():
            source = await aiosqlite.connect(f"{legacy_path.as_uri()}?mode=ro", uri=True)
            destination = await aiosqlite.connect(str(requested_path))
            try:
                await source.backup(destination)
            finally:
                await destination.close()
                await source.close()
    else:
        requested_path.parent.mkdir(parents=True, exist_ok=True)

    return await aiosqlite.connect(str(requested_path))


class _PostgresCursor:
    def __init__(self, cursor: Any, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    async def fetchone(self):
        try:
            return await self._cursor.fetchone()
        except Exception as error:
            _raise_compatible_error(error)

    async def fetchall(self):
        try:
            return await self._cursor.fetchall()
        except Exception as error:
            _raise_compatible_error(error)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._cursor.close()


class _PostgresExecuteRequest:
    def __init__(self, connection: "_PostgresConnection", sql: str, parameters: Any):
        self._connection = connection
        self._sql = sql
        self._parameters = parameters
        self._cursor: _PostgresCursor | None = None

    async def _run(self):
        if self._cursor is None:
            try:
                self._cursor = await self._connection._execute(
                    self._sql, self._parameters
                )
            except Exception as error:
                _raise_compatible_error(error)
        return self._cursor

    def __await__(self):
        return self._run().__await__()

    async def __aenter__(self):
        return await self._run()

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self._cursor is not None:
            await self._cursor.__aexit__(exc_type, exc_value, traceback)


class _PostgresConnection:
    def __init__(self, connection: Any):
        self._connection = connection
        self._lock = asyncio.Lock()
        self._serial_columns: dict[str, str | None] = {}

    @classmethod
    async def connect(cls, database_url: str):
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL support requires psycopg. Install requirements.txt."
            ) from error
        connection = await psycopg.AsyncConnection.connect(
            database_url,
            autocommit=True,
        )
        return cls(connection)

    async def _primary_key_columns(self, table_name: str) -> list[str]:
        async with self._lock:
            async with self._connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT key_column.column_name
                    FROM information_schema.table_constraints AS table_constraint
                    JOIN information_schema.key_column_usage AS key_column
                      ON key_column.constraint_name = table_constraint.constraint_name
                     AND key_column.table_schema = table_constraint.table_schema
                     AND key_column.table_name = table_constraint.table_name
                    WHERE table_constraint.constraint_type = 'PRIMARY KEY'
                      AND table_constraint.table_schema = current_schema()
                      AND table_constraint.table_name = %s
                    ORDER BY key_column.ordinal_position
                    """,
                    (table_name,),
                )
                return [row[0] for row in await cursor.fetchall()]

    async def _serial_column(self, table_name: str) -> str | None:
        if table_name not in self._serial_columns:
            async with self._lock:
                async with self._connection.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = %s
                          AND column_default LIKE 'nextval(%%'
                        ORDER BY ordinal_position
                        LIMIT 1
                        """,
                        (table_name,),
                    )
                    row = await cursor.fetchone()
            self._serial_columns[table_name] = row[0] if row else None
        return self._serial_columns[table_name]

    async def _translate_insert(self, sql: str) -> tuple[str, str | None]:
        prefix = _INSERT_PREFIX.match(sql)
        plain_insert = prefix is None
        lastrowid_column = None
        if prefix is not None:
            operation = prefix.group(1).upper()
            sql = sql[:prefix.start()] + "INSERT INTO " + sql[prefix.end():]
            columns_match = _INSERT_TABLE_AND_COLUMNS.match(sql)
            if operation == "IGNORE":
                if not re.search(r"\bON\s+CONFLICT\b", sql, re.IGNORECASE):
                    sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
            elif columns_match is not None:
                table_name = columns_match.group(1)
                columns = [column.strip().strip('"') for column in columns_match.group(2).split(",")]
                primary_keys = await self._primary_key_columns(table_name)
                if not primary_keys:
                    raise ValueError(f"Cannot replace rows in {table_name}: no primary key found.")
                updates = [column for column in columns if column not in primary_keys]
                conflict_target = ", ".join(primary_keys)
                if updates:
                    update_clause = ", ".join(
                        f"{column}=EXCLUDED.{column}" for column in updates
                    )
                    sql = (
                        sql.rstrip().rstrip(";")
                        + f" ON CONFLICT ({conflict_target}) DO UPDATE SET {update_clause}"
                    )
                else:
                    sql = sql.rstrip().rstrip(";") + f" ON CONFLICT ({conflict_target}) DO NOTHING"

        insert_match = _AUTOINCREMENT_COLUMN.match(sql)
        if (
            plain_insert
            and insert_match is not None
            and not re.search(r"\bRETURNING\b", sql, re.IGNORECASE)
        ):
            table_name = insert_match.group(1)
            inserted_columns = {
                column.strip().strip('"')
                for column in insert_match.group(2).split(",")
            }
            serial_column = await self._serial_column(table_name)
            if serial_column and serial_column not in inserted_columns:
                lastrowid_column = serial_column
                sql = sql.rstrip().rstrip(";") + f" RETURNING {serial_column}"
        return sql, lastrowid_column

    async def _execute(self, sql: str, parameters: Any = None) -> _PostgresCursor:
        pragma_match = _PRAGMA_TABLE_INFO.match(sql)
        if pragma_match:
            table_name = pragma_match.group(1)
            sql = """
                SELECT ordinal_position - 1 AS cid,
                       column_name AS name,
                       data_type AS type,
                       (is_nullable = 'NO') AS notnull,
                       column_default AS dflt_value,
                       CASE WHEN column_name IN (
                           SELECT key_column.column_name
                           FROM information_schema.table_constraints AS table_constraint
                           JOIN information_schema.key_column_usage AS key_column
                             ON key_column.constraint_name = table_constraint.constraint_name
                            AND key_column.table_schema = table_constraint.table_schema
                            AND key_column.table_name = table_constraint.table_name
                           WHERE table_constraint.constraint_type = 'PRIMARY KEY'
                             AND table_constraint.table_schema = current_schema()
                             AND key_column.table_name = information_schema.columns.table_name
                       ) THEN 1 ELSE 0 END AS pk
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position
            """
            parameters = (table_name,)
        else:
            sql, returning_column = await self._translate_insert(sql)
            if returning_column:
                sql = sql.replace("?", "%s")
                async with self._lock:
                    cursor = self._connection.cursor()
                    await cursor.execute(sql, parameters)
                    row = await cursor.fetchone()
                    return _PostgresCursor(cursor, row[0] if row else None)

        sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\b", "BIGINT PRIMARY KEY", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bINTEGER\b", "BIGINT", sql, flags=re.IGNORECASE)
        sql = _quote_postgres_reserved_identifiers(sql)
        sql = re.sub(r"\bdatetime\s*\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "BEGIN", sql, flags=re.IGNORECASE)
        sql = sql.replace("?", "%s")

        async with self._lock:
            cursor = self._connection.cursor()
            try:
                await cursor.execute(sql, parameters)
            except Exception as error:
                _raise_compatible_error(error)
            return _PostgresCursor(cursor)

    def execute(self, sql: str, parameters: Any = None):
        return _PostgresExecuteRequest(self, sql, parameters)

    async def executemany(self, sql: str, parameters: Any):
        sql, _ = await self._translate_insert(sql)
        sql = re.sub(r"\bINTEGER\b", "BIGINT", sql, flags=re.IGNORECASE)
        sql = sql.replace("?", "%s")
        async with self._lock:
            cursor = self._connection.cursor()
            try:
                await cursor.executemany(sql, parameters)
            except Exception as error:
                _raise_compatible_error(error)
            return _PostgresCursor(cursor)

    async def executescript(self, script: str):
        for statement in script.split(";"):
            if statement.strip():
                try:
                    await self._execute(statement)
                except Exception as error:
                    _raise_compatible_error(error)

    async def commit(self):
        try:
            await self._connection.commit()
        except Exception as error:
            _raise_compatible_error(error)

    async def rollback(self):
        try:
            await self._connection.rollback()
        except Exception as error:
            _raise_compatible_error(error)

    async def close(self):
        await self._connection.close()


def _raise_compatible_error(error: Exception):
    try:
        import psycopg
    except ImportError:
        raise error
    if isinstance(error, psycopg.IntegrityError):
        raise aiosqlite.IntegrityError(str(error)) from error
    if isinstance(error, psycopg.OperationalError):
        raise aiosqlite.OperationalError(str(error)) from error
    if isinstance(error, psycopg.Error):
        raise aiosqlite.Error(str(error)) from error
    raise error
