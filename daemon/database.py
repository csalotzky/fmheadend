"""Versioned SQLite configuration repository. No runtime state is stored here."""
import json
import sqlite3
from pathlib import Path

from .errors import DomainError
from .models.configuration import BasicConfig, InputSource, Modulator, ModulatorConfig, PsEntry, SystemConfig, utcnow

SCHEMA_VERSION = 2
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
        if version not in (0, 1, SCHEMA_VERSION):
            raise RuntimeError(f"Unsupported database schema {version}; database left untouched.")
        if version == 1:
            self._migrate_profiles()
        if version == 0:
            # Explicit transaction includes all DDL, seeds and the version bump.
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for statement in (
                    "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)",
                    "CREATE TABLE modulators (id INTEGER PRIMARY KEY CHECK(id BETWEEN 0 AND 7), input_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT, data TEXT NOT NULL)",
                    "CREATE TABLE system_config (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL)",
                    "CREATE TRIGGER fixed_modulator_delete BEFORE DELETE ON modulators BEGIN SELECT RAISE(ABORT, 'Modulators are fixed'); END",
                    "CREATE TRIGGER fixed_modulator_id BEFORE UPDATE OF id ON modulators BEGIN SELECT RAISE(ABORT, 'Modulator IDs are fixed'); END",
                ):
                    self.connection.execute(statement)
                for modulator_id in range(8):
                    config = ModulatorConfig(rds_ps=[PsEntry(text=f"FM TX {modulator_id}")])
                    data = config.model_dump(mode="json") | {"timestamp": utcnow().isoformat()}
                    self.connection.execute("INSERT INTO modulators VALUES (?, NULL, ?)", (modulator_id, json.dumps(data)))
                self.connection.execute("INSERT INTO system_config VALUES (1, ?)", (SystemConfig().model_dump_json(),))
                self.connection.execute("PRAGMA user_version = 2")
        ids = [row[0] for row in self.connection.execute("SELECT id FROM modulators ORDER BY id")]
        if ids != list(range(8)):
            raise RuntimeError("Database must contain exactly modulators 0..7")

    def _migrate_profiles(self):
        """One-time, transactional v1 conversion. Unknown data is never discarded."""
        self.connection.execute('PRAGMA foreign_keys = OFF')
        try:
            self.connection.execute('BEGIN IMMEDIATE')
            with self.connection:
                if [r[0] for r in self.connection.execute('SELECT id FROM modulators ORDER BY id')] != list(range(8)):
                    raise ValueError('Database must contain exactly modulators 0..7')
                profiles = {row['id']: json.loads(row['data']) for row in
                            self.connection.execute('SELECT * FROM audio_profiles')}
                converted, used = [], set()
                for row in self.connection.execute('SELECT * FROM sources'):
                    data = json.loads(row['data'])
                    pid = data.pop('audio_profile_id', None)
                    if pid != row['audio_profile_id']:
                        raise ValueError(f"Source {row['id']} profile reference disagrees with its foreign key")
                    data.update(processor_mode='bypass', input_gain_db=0, audio_profile_path=None)
                    if pid is not None:
                        if pid not in profiles:
                            raise ValueError(f"Source {row['id']} references missing profile {pid}")
                        p = profiles[pid]
                        if not isinstance(p, dict) or not isinstance(p.get('name'), str) or not p['name'].strip():
                            raise ValueError(f'Profile {pid} has invalid content/name')
                        allowed = {'name', 'gain_db', 'processor', 'basic_config', 'eq_config',
                                   'compressor_config', 'limiter_config', 'advanced_options', 'timestamp'}
                        if set(p) - allowed or any(p.get(k) for k in ('eq_config', 'compressor_config',
                                                                     'limiter_config', 'advanced_options')):
                            raise ValueError(f'Profile {pid} contains unsupported settings')
                        basic = BasicConfig.model_validate(p.get('basic_config', {}))
                        if basic != BasicConfig():
                            raise ValueError(f'Profile {pid} has custom Basic settings; export it before migration')
                        processor = p.get('processor', 'NONE')
                        if processor not in ('NONE', 'GST_BASIC'):
                            raise ValueError(f'Profile {pid} has unknown processor {processor}')
                        data['input_gain_db'] = p.get('gain_db', 0)
                        if processor == 'GST_BASIC':
                            data.update(processor_mode='builtin', audio_profile_path='default/basic.json')
                        used.add(pid)
                    validated = InputSource.model_validate(data | {'id': row['id']})
                    converted.append((row['id'], validated.model_dump_json(exclude={'id'})))
                if set(profiles) - used:
                    raise ValueError(f'Unreferenced profiles {sorted(set(profiles) - used)} require explicit export/removal')
                sequence = self.connection.execute("SELECT seq FROM sqlite_sequence WHERE name='sources'").fetchone()
                self.connection.execute('CREATE TABLE sources_new (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)')
                self.connection.executemany('INSERT INTO sources_new VALUES (?, ?)', converted)
                self.connection.execute('DROP TABLE sources')
                self.connection.execute('ALTER TABLE sources_new RENAME TO sources')
                if sequence:
                    self.connection.execute("DELETE FROM sqlite_sequence WHERE name='sources'")
                    self.connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('sources',?)", (sequence[0],))
                self.connection.execute('DROP TABLE audio_profiles')
                if self.connection.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('Unresolved database references')
                self.connection.execute('PRAGMA user_version = 2')
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            raise RuntimeError(f'Audio profile migration blocked; database unchanged: {exc}') from exc
        finally:
            self.connection.execute('PRAGMA foreign_keys = ON')

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

    def close(self):
        self.connection.close()
