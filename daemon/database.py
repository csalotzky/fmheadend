"""Versioned SQLite configuration repository. No runtime state is stored here."""
import json
import sqlite3
from pathlib import Path

from .errors import DomainError
from .models.configuration import InputSource, Modulator, ModulatorConfig, PsEntry, SystemConfig, utcnow

SCHEMA_VERSION = 1
SCHEMA_STATEMENTS = (
    "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)",
    "CREATE TABLE modulators (id INTEGER PRIMARY KEY CHECK(id BETWEEN 0 AND 7), input_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT, data TEXT NOT NULL)",
    "CREATE TABLE system_config (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL)",
    "CREATE TRIGGER fixed_modulator_delete BEFORE DELETE ON modulators BEGIN SELECT RAISE(ABORT, 'Modulators are fixed'); END",
    "CREATE TRIGGER fixed_modulator_id BEFORE UPDATE OF id ON modulators BEGIN SELECT RAISE(ABORT, 'Modulator IDs are fixed'); END",
)
RECORDS = {"sources": InputSource, "modulators": Modulator}


class Database:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _initialize(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(f"Unsupported database schema {version}; database left untouched.")
        if version == 0:
            if self.connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise RuntimeError("Unversioned nonempty database; database left untouched.")
            # Explicit transaction includes all DDL, seeds and the version bump.
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for statement in SCHEMA_STATEMENTS:
                    self.connection.execute(statement)
                for modulator_id in range(8):
                    config = ModulatorConfig(rds_ps=[PsEntry(text=f"FM TX {modulator_id}")])
                    data = config.model_dump(mode="json") | {"timestamp": utcnow().isoformat()}
                    self.connection.execute("INSERT INTO modulators VALUES (?, NULL, ?)", (modulator_id, json.dumps(data)))
                self.connection.execute("INSERT INTO system_config VALUES (1, ?)", (SystemConfig().model_dump_json(),))
                self.connection.execute("PRAGMA user_version = 1")
        expected_columns = {'sources': ['id', 'data'],
                            'modulators': ['id', 'input_source_id', 'data'],
                            'system_config': ['id', 'data'], 'sqlite_sequence': ['name', 'seq']}
        tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != set(expected_columns) or any(
                [r[1] for r in self.connection.execute(f'PRAGMA table_info({table})')] != columns
                for table, columns in expected_columns.items()):
            raise RuntimeError("Incorrect database schema 1 structure; database left untouched.")
        ids = [row[0] for row in self.connection.execute("SELECT id FROM modulators ORDER BY id")]
        if ids != list(range(8)):
            raise RuntimeError("Database must contain exactly modulators 0..7")
        # Identifier quoting/whitespace is not a structural change.
        def normalize(sql):
            return ' '.join(sql.replace('"', '').split())
        actual = {normalize(r[0]) for r in self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if actual != {normalize(sql) for sql in SCHEMA_STATEMENTS}:
            raise RuntimeError("Incorrect database schema 1 structure; database left untouched.")

    def list(self, table):
        model = RECORDS[table]  # table names are internal, never supplied by API callers
        return [model.model_validate(json.loads(row["data"]) | {"id": row["id"]})
                for row in self.connection.execute(f"SELECT id, data FROM {table} ORDER BY id")]

    def get(self, table, record_id):
        model = RECORDS[table]
        row = self.connection.execute(f"SELECT id, data FROM {table} WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise DomainError(404, "NOT_FOUND", f"{table} resource {record_id} does not exist")
        return model.model_validate(json.loads(row["data"]) | {"id": row["id"]})

    def save(self, table, config, record_id=None):
        if table not in RECORDS or (table == "modulators" and record_id is None):
            raise ValueError("Invalid repository operation")
        if record_id is not None:
            self.get(table, record_id)
        data = config.model_dump(mode="json", exclude={"id", "timestamp"}) | {"timestamp": utcnow().isoformat()}
        columns, values = ["data"], [json.dumps(data)]
        relation = {"modulators": "input_source_id"}.get(table)
        if relation:
            columns.append(relation)
            values.append(data[relation])
        try:
            with self.connection:
                if record_id is None:
                    cursor = self.connection.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)
                    record_id = cursor.lastrowid
                else:
                    self.connection.execute(
                        f"UPDATE {table} SET {', '.join(c + ' = ?' for c in columns)} WHERE id = ?", values + [record_id])
        except sqlite3.IntegrityError as exc:
            raise DomainError(409, "REFERENCE_CONFLICT", "Referenced resource does not exist") from exc
        return self.get(table, record_id)

    def delete(self, table, record_id):
        if table != "sources":
            raise ValueError("Resource cannot be deleted")
        self.get(table, record_id)
        try:
            with self.connection:
                self.connection.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
        except sqlite3.IntegrityError as exc:
            raise DomainError(409, "RESOURCE_IN_USE", "Resource is referenced by another configuration") from exc

    def get_system(self):
        return SystemConfig.model_validate_json(self.connection.execute("SELECT data FROM system_config WHERE id = 1").fetchone()[0])

    def save_system(self, config):
        with self.connection:
            self.connection.execute("UPDATE system_config SET data = ? WHERE id = 1", (config.model_dump_json(),))
        return config

    def select_single_modulator(self, modulator_id):
        """Atomically replace a globally stopped selection with one desired TX."""
        self.get("modulators", modulator_id)  # Reject invalid IDs before writing.
        # Do not call save/save_system here: their contexts commit independently.
        with self.connection:
            for row in self.connection.execute("SELECT id, data FROM modulators ORDER BY id").fetchall():
                data = json.loads(row["data"])
                enabled = row["id"] == modulator_id
                if data["enabled"] != enabled:
                    data.update(enabled=enabled, timestamp=utcnow().isoformat())
                    self.connection.execute("UPDATE modulators SET data = ? WHERE id = ?",
                                            (json.dumps(data), row["id"]))
            system = self.get_system().model_copy(update={"global_stopped": False})
            self.connection.execute("UPDATE system_config SET data = ? WHERE id = 1",
                                    (system.model_dump_json(),))

    def close(self):
        self.connection.close()
