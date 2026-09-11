"""Versioned SQLite configuration repository. No runtime state is stored here."""
import json
import sqlite3
from pathlib import Path

from .errors import DomainError
from .models.configuration import AudioProfile, InputSource, Modulator, ModulatorConfig, SystemConfig, utcnow

SCHEMA_VERSION = 2
RECORDS = {"sources": InputSource, "audio_profiles": AudioProfile, "modulators": Modulator}


class Database:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        try:
            self._migrate()
        except Exception:
            self.close()
            raise

    def _migrate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}")
        if version == 0:
            # Explicit transaction includes all DDL, seeds and the version bump.
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for statement in (
                    "CREATE TABLE audio_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)",
                    "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, audio_profile_id INTEGER REFERENCES audio_profiles(id) ON DELETE RESTRICT, data TEXT NOT NULL)",
                    "CREATE TABLE modulators (id INTEGER PRIMARY KEY CHECK(id BETWEEN 0 AND 7), input_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT, data TEXT NOT NULL)",
                    "CREATE TABLE system_config (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL)",
                    "CREATE TRIGGER fixed_modulator_delete BEFORE DELETE ON modulators BEGIN SELECT RAISE(ABORT, 'Modulators are fixed'); END",
                    "CREATE TRIGGER fixed_modulator_id BEFORE UPDATE OF id ON modulators BEGIN SELECT RAISE(ABORT, 'Modulator IDs are fixed'); END",
                ):
                    self.connection.execute(statement)
                for modulator_id in range(8):
                    config = ModulatorConfig(name=f"TX{modulator_id}")
                    data = config.model_dump(mode="json") | {"timestamp": utcnow().isoformat()}
                    self.connection.execute("INSERT INTO modulators VALUES (?, NULL, ?)", (modulator_id, json.dumps(data)))
                self.connection.execute("INSERT INTO system_config VALUES (1, ?)", (SystemConfig().model_dump_json(),))
                self.connection.execute("PRAGMA user_version = 1")
        if version < 2:
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for row in self.connection.execute("SELECT id, data FROM modulators").fetchall():
                    data = json.loads(row["data"])
                    # Legacy raw values have no reliable physical-unit conversion.
                    data.pop("rf_level", None)
                    data.setdefault("rf_output_dbuv", None)
                    self.connection.execute("UPDATE modulators SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
                self.connection.execute("PRAGMA user_version = 2")
        ids = [row[0] for row in self.connection.execute("SELECT id FROM modulators ORDER BY id")]
        if ids != list(range(8)):
            raise RuntimeError("Database must contain exactly modulators 0..7")

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
        relation = {"sources": "audio_profile_id", "modulators": "input_source_id"}.get(table)
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
        if table not in ("sources", "audio_profiles"):
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

    def close(self):
        self.connection.close()
