"""Versioned SQLite configuration repository. No runtime state is stored here."""
import json
import sqlite3
from pathlib import Path

from .errors import DomainError
from .models.configuration import AudioProfile, InputSource, Modulator, ModulatorConfig, SystemConfig, utcnow

SCHEMA_VERSION = 8
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
                    config = ModulatorConfig()
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
        if version < 3:
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for row in self.connection.execute("SELECT id, data FROM modulators").fetchall():
                    data = json.loads(row["data"])
                    data.setdefault("rds_ecc", None)
                    data.setdefault("rds_af_khz", [])
                    self.connection.execute("UPDATE modulators SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
                self.connection.execute("PRAGMA user_version = 3")
        if version < 4:
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for row in self.connection.execute("SELECT id, data FROM modulators").fetchall():
                    data = json.loads(row["data"])
                    # Physical ID is the only modulator identity. Preserve every
                    # other value, including the original configuration timestamp.
                    data.pop("name", None)
                    self.connection.execute("UPDATE modulators SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
                self.connection.execute("PRAGMA user_version = 4")
        if version < 5:
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                analog_ids = {row["id"] for row in self.connection.execute("SELECT id, data FROM sources")
                              if json.loads(row["data"]).get("type") == "ANALOG"}
                for row in self.connection.execute("SELECT id, input_source_id, data FROM modulators").fetchall():
                    data = json.loads(row["data"])
                    source_id = data.get("input_source_id")
                    if source_id != row["input_source_id"]:
                        raise RuntimeError(f"Modulator {row['id']} has conflicting legacy source references; migration aborted")
                    if source_id in analog_ids:
                        data["audio_mode"] = "ANALOG"
                        data["input_source_id"] = None
                    else:
                        data.setdefault("audio_mode", "DIGITAL")
                        if data["audio_mode"] == "ANALOG" and source_id is not None:
                            raise RuntimeError("Conflicting legacy audio mode and digital source; migration aborted")
                    self.connection.execute("UPDATE modulators SET input_source_id = ?, data = ? WHERE id = ?",
                                            (data.get("input_source_id"), json.dumps(data), row["id"]))
                for source_id in analog_ids:
                    self.connection.execute("DELETE FROM sources WHERE id = ?", (source_id,))
                self.connection.execute("PRAGMA user_version = 5")
        if version < 6:
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for row in self.connection.execute("SELECT id, data FROM sources").fetchall():
                    data = json.loads(row["data"])
                    if data.get("type") != "HTTP" or "user_agent" not in data["config"]:
                        continue
                    legacy = data["config"].pop("user_agent")
                    options = data.setdefault("gst_options", {}).setdefault("source", {})
                    # Preserve the old effective value: advanced overrides were applied last.
                    options.setdefault("user-agent", legacy)
                    self.connection.execute("UPDATE sources SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
                self.connection.execute("PRAGMA user_version = 6")
        if version < 7:
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for row in self.connection.execute("SELECT id, data FROM modulators").fetchall():
                    data = json.loads(row["data"])
                    for key, default in {"gain_mode": "AUTO", "analog_gain_db": 0.0,
                                         "digital_trim_db": 0, "soft_clipping": False}.items():
                        data.setdefault(key, default)
                    self.connection.execute("UPDATE modulators SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
                self.connection.execute("PRAGMA user_version = 7")
        if version < 8:
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for row in self.connection.execute("SELECT id, data FROM sources").fetchall():
                    data = json.loads(row["data"])
                    data.pop("enabled", None)
                    self.connection.execute("UPDATE sources SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
                self.connection.execute("PRAGMA user_version = 8")
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
