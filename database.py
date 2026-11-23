from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import DictCursor
except ImportError:
    psycopg2 = None
    pool = None
    DictCursor = None

from flower_explorer import FlowerRecord, MEASUREMENTS, SPECIES
from flower_web.security import hash_password


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flowers (
    id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    sepal_length_cm REAL NOT NULL,
    sepal_width_cm REAL NOT NULL,
    petal_length_cm REAL NOT NULL,
    petal_width_cm REAL NOT NULL,
    species TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flowers_owner ON flowers(user_id);
CREATE INDEX IF NOT EXISTS idx_flowers_species ON flowers(species);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    file_format TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    imported INTEGER NOT NULL,
    invalid INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    analysis_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_imports (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    duplicate_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (user_id, token_hash)
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flowers (
    id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    sepal_length_cm DOUBLE PRECISION NOT NULL,
    sepal_width_cm DOUBLE PRECISION NOT NULL,
    petal_length_cm DOUBLE PRECISION NOT NULL,
    petal_width_cm DOUBLE PRECISION NOT NULL,
    species TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flowers_owner ON flowers(user_id);
CREATE INDEX IF NOT EXISTS idx_flowers_species ON flowers(species);
CREATE TABLE IF NOT EXISTS datasets (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    file_format TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_history (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    imported INTEGER NOT NULL,
    invalid INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_history (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    analysis_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_imports (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    duplicate_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (user_id, token_hash)
);
"""

_SORT_COLUMNS = {
    "created_at": "created_at",
    "species": "COALESCE(species, '')",
    "sepal_length": "sepal_length_cm",
    "petal_length": "petal_length_cm",
}


class _PostgresCursor:
    def __init__(self, connection):
        self._cursor = connection.cursor(cursor_factory=DictCursor)
        self.rowcount = 0

    def execute(self, query: str, parameters=None):
        query = query.replace("?", "%s")
        query = query.replace(" COLLATE NOCASE", "")
        if query.startswith("PRAGMA ") or "PRAGMA " in query:
            return self
        if parameters is not None:
            self._cursor.execute(query, tuple(parameters))
        else:
            self._cursor.execute(query)
        self.rowcount = self._cursor.rowcount
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()


class _PostgresConnection:
    def __init__(self, dsn: str):
        if psycopg2 is None:
            raise RuntimeError("PostgreSQL support requires psycopg2-binary.")
        if "sslmode=" not in dsn:
            dsn = f"{dsn}{'&' if '?' in dsn else '?'}sslmode=require"
        self._connection = psycopg2.connect(dsn)
        self.row_factory = DictCursor

    def execute(self, query: str, parameters=None):
        cursor = _PostgresCursor(self._connection)
        return cursor.execute(query, parameters)

    def executescript(self, script: str):
        cursor = self._connection.cursor()
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                cursor.execute(statement)
        cursor.close()

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


class Database:
    def __init__(self, path: Path, seed_json: Path | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seed_json = seed_json
        self.postgres_dsn = (
            os.environ.get("FLOWERDB_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or os.environ.get("POSTGRES_URL")
            or os.environ.get("POSTGRES_PRISMA_URL")
        )
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.postgres_dsn:
            connection = _PostgresConnection(self.postgres_dsn)
        else:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(POSTGRES_SCHEMA if self.postgres_dsn else SCHEMA)
            existing = connection.execute("SELECT COUNT(*) FROM flowers").fetchone()[0]
            if existing:
                return
            records: list[FlowerRecord] = []
            if self.seed_json and self.seed_json.exists():
                payload = json.loads(self.seed_json.read_text(encoding="utf-8"))
                for row in payload.get("records", []):
                    records.append(FlowerRecord.from_mapping(row))
            for record in records:
                self._insert_record(connection, record, None)
            if records:
                connection.execute(
                    """INSERT INTO datasets
                       (user_id, file_name, file_format, row_count, imported_at)
                       VALUES (NULL, ?, ?, ?, ?)""",
                    (
                        self.seed_json.with_suffix(".csv").name,
                        "csv",
                        len(records),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )

    @staticmethod
    def _insert_record(
        connection: sqlite3.Connection, record: FlowerRecord, user_id: int | None
    ) -> None:
        connection.execute(
            """INSERT INTO flowers
               (id, user_id, sepal_length_cm, sepal_width_cm, petal_length_cm,
                petal_width_cm, species, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id,
                user_id,
                record.sepal_length_cm,
                record.sepal_width_cm,
                record.petal_length_cm,
                record.petal_width_cm,
                record.species,
                record.created_at,
            ),
        )

    @staticmethod
    def _record_dict(row: sqlite3.Row) -> dict[str, object]:
        record = {key: row[key] for key in ("id", *MEASUREMENTS, "species", "created_at")}
        record["_owner_id"] = row["user_id"] if "user_id" in row.keys() else None
        return record

    def register(
        self, full_name: str, email: str, username: str, password: str
    ) -> int:
        try:
            with self.connection() as connection:
                cursor = connection.execute(
                    """INSERT INTO users (full_name, email, username, password_hash, created_at)
                       VALUES (?, ?, ?, ?, ?) RETURNING id""",
                    (
                        full_name,
                        email,
                        username,
                        hash_password(password),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                row = cursor.fetchone()
                if isinstance(row, dict):
                    return int(row["id"])
                return int(row[0] if row else cursor.lastrowid)
        except (sqlite3.IntegrityError, psycopg2.IntegrityError if psycopg2 else sqlite3.IntegrityError) as exc:
            raise ValueError("That email or username is already registered.") from exc

    def user_for_login(self, identifier: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            if self.postgres_dsn:
                return connection.execute(
                    """SELECT id, full_name, email, username, password_hash
                       FROM users
                       WHERE LOWER(email) = LOWER(?) OR LOWER(username) = LOWER(?)""",
                    (identifier, identifier),
                ).fetchone()
            return connection.execute(
                """SELECT id, full_name, email, username, password_hash
                   FROM users WHERE email = ? OR username = ?""",
                (identifier, identifier),
            ).fetchone()

    def get_user(self, user_id: int) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, full_name, email, username, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_profile(self, user_id: int, full_name: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE users SET full_name = ? WHERE id = ?", (full_name, user_id)
            )

    def update_password(self, user_id: int, password: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password), user_id),
            )

    def save_record(
        self, user_id: int, values: dict[str, object], record_id: str | None = None
    ) -> str:
        if record_id:
            with self.connection() as connection:
                existing = connection.execute(
                    "SELECT 1 FROM flowers WHERE id = ? AND user_id = ?",
                    (record_id, user_id),
                ).fetchone()
                if not existing:
                    raise LookupError("Record not found.")
                connection.execute(
                    """UPDATE flowers SET sepal_length_cm = ?, sepal_width_cm = ?,
                       petal_length_cm = ?, petal_width_cm = ?, species = ? WHERE id = ?""",
                    (
                        values["sepal_length_cm"],
                        values["sepal_width_cm"],
                        values["petal_length_cm"],
                        values["petal_width_cm"],
                        values.get("species") or None,
                        record_id,
                    ),
                )
            return record_id
        record = FlowerRecord.create(
            {name: float(values[name]) for name in MEASUREMENTS},
            str(values["species"]) if values.get("species") else None,
        )
        with self.connection() as connection:
            self._insert_record(connection, record, user_id)
        return record.id

    def get_record(self, user_id: int, record_id: str) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM flowers WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (record_id, user_id),
            ).fetchone()
            return self._record_dict(row) if row else None

    def delete_record(self, user_id: int, record_id: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM flowers WHERE id = ? AND user_id = ?",
                (record_id, user_id),
            )
            return cursor.rowcount > 0

    def records(
        self,
        user_id: int,
        *,
        query: str = "",
        species: str = "",
        sort: str = "created_at",
        descending: bool = True,
        page: int = 1,
        per_page: int = 12,
    ) -> tuple[list[dict[str, object]], int]:
        conditions = ["(user_id = ? OR user_id IS NULL)"]
        parameters: list[object] = [user_id]
        if species:
            conditions.append("species = ?" if species != "Unlabeled" else "species IS NULL")
            if species != "Unlabeled":
                parameters.append(species)
        if query.strip():
            like = f"%{query.strip()}%"
            conditions.append(
                "(id LIKE ? OR COALESCE(species, 'Unlabeled') LIKE ? OR "
                "CAST(sepal_length_cm AS TEXT) LIKE ? OR CAST(sepal_width_cm AS TEXT) LIKE ? "
                "OR CAST(petal_length_cm AS TEXT) LIKE ? OR CAST(petal_width_cm AS TEXT) LIKE ?)"
            )
            parameters.extend([like] * 6)
        where = " AND ".join(conditions)
        safe_sort = _SORT_COLUMNS.get(sort, "created_at")
        order = "DESC" if descending else "ASC"
        offset = (max(page, 1) - 1) * per_page
        with self.connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM flowers WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM flowers WHERE {where} ORDER BY {safe_sort} {order}, id LIMIT ? OFFSET ?",
                [*parameters, per_page, offset],
            ).fetchall()
            return [self._record_dict(row) for row in rows], total

    def all_records(self, user_id: int, species: str = "") -> list[dict[str, object]]:
        conditions = ["(user_id = ? OR user_id IS NULL)"]
        parameters: list[object] = [user_id]
        if species and species in SPECIES:
            conditions.append("species = ?")
            parameters.append(species)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM flowers WHERE {' AND '.join(conditions)} ORDER BY created_at, id",
                parameters,
            ).fetchall()
            return [self._record_dict(row) for row in rows]

    def metrics(self, user_id: int) -> dict[str, object]:
        columns = ", ".join(f"AVG({name}) AS avg_{name}" for name in MEASUREMENTS)
        with self.connection() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS total, COUNT(DISTINCT species) AS classes,
                    MAX(created_at) AS latest, {columns} FROM flowers
                    WHERE user_id = ? OR user_id IS NULL""",
                (user_id,),
            ).fetchone()
            distribution = connection.execute(
                """SELECT COALESCE(species, 'Unlabeled') AS species, COUNT(*) AS count
                   FROM flowers WHERE user_id = ? OR user_id IS NULL
                   GROUP BY species ORDER BY species""",
                (user_id,),
            ).fetchall()
            return {
                **dict(row),
                "distribution": {item["species"]: item["count"] for item in distribution},
            }

    def dataset_count(self, user_id: int) -> int:
        with self.connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM datasets WHERE user_id = ? OR user_id IS NULL",
                (user_id,),
            ).fetchone()[0]

    def datasets(self, user_id: int, limit: int = 20) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT file_name, file_format, row_count, imported_at
                   FROM datasets WHERE user_id = ? OR user_id IS NULL
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def import_history(self, user_id: int, limit: int = 20) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT file_name, imported, invalid, duplicates, created_at
                   FROM import_history WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def analysis_log(self, user_id: int, analysis_type: str, summary: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO analysis_history (user_id, analysis_type, summary, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    user_id,
                    analysis_type,
                    summary,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )

    def stage_import(
        self,
        user_id: int,
        file_name: str,
        rows: list[dict[str, object]],
        errors: list[str],
        duplicate_count: int,
    ) -> str:
        import_id = uuid.uuid4().hex
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO pending_imports
                   (id, user_id, file_name, rows_json, errors_json, duplicate_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    import_id,
                    user_id,
                    file_name,
                    json.dumps(rows),
                    json.dumps(errors),
                    duplicate_count,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        return import_id

    def pending_import(self, user_id: int, import_id: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM pending_imports WHERE id = ? AND user_id = ?",
                (import_id, user_id),
            ).fetchone()

    def commit_import(self, user_id: int, import_id: str) -> dict[str, object]:
        with self.connection() as connection:
            pending = connection.execute(
                "SELECT * FROM pending_imports WHERE id = ? AND user_id = ?",
                (import_id, user_id),
            ).fetchone()
            if not pending:
                raise LookupError("Import preview expired or not found.")
            rows = json.loads(pending["rows_json"])
            errors = json.loads(pending["errors_json"])
            existing = connection.execute(
                "SELECT id, sepal_length_cm, sepal_width_cm, petal_length_cm, petal_width_cm, species "
                "FROM flowers WHERE user_id = ? OR user_id IS NULL",
                (user_id,),
            ).fetchall()
            existing_ids = {
                row["id"]: tuple(
                    [*(round(row[name], 2) for name in MEASUREMENTS), row["species"]]
                )
                for row in existing
            }
            inserted = 0
            duplicates = int(pending["duplicate_count"])
            for row in rows:
                record = FlowerRecord.from_mapping(row)
                signature = tuple(
                    [
                        *(round(getattr(record, name), 2) for name in MEASUREMENTS),
                        record.species,
                    ]
                )
                if record.id in existing_ids:
                    if signature == existing_ids[record.id]:
                        duplicates += 1
                    else:
                        errors.append(
                            f"Record ID '{record.id}' already exists with different measurements."
                        )
                    continue
                self._insert_record(connection, record, user_id)
                existing_ids[record.id] = signature
                inserted += 1
            invalid = len(errors)
            connection.execute(
                """INSERT INTO datasets (user_id, file_name, file_format, row_count, imported_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    user_id,
                    pending["file_name"],
                    Path(pending["file_name"]).suffix.lower().lstrip("."),
                    inserted,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            connection.execute(
                """INSERT INTO import_history
                   (user_id, file_name, imported, invalid, duplicates, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    pending["file_name"],
                    inserted,
                    invalid,
                    duplicates,
                    json.dumps(errors),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            connection.execute("DELETE FROM pending_imports WHERE id = ?", (import_id,))
            return {
                "imported": inserted,
                "invalid": invalid,
                "duplicates": duplicates,
                "errors": errors,
            }

    def create_reset_token(self, user_id: int, token_hash: str) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        with self.connection() as connection:
            connection.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
            connection.execute(
                "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, token_hash, expires),
            )

    def consume_reset_token(self, token_hash: str) -> int | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT user_id FROM password_resets WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "DELETE FROM password_resets WHERE user_id = ?", (row["user_id"],)
            )
            return int(row["user_id"])

