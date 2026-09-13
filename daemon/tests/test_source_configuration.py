"""Demand-driven source configuration and schema-7 migration coverage."""
import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig


def test_schema7_source_flag_removal_preserves_records(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    expected = {}
    with db.connection:
        for flag in (True, False, None):
            source = db.save("sources", InputSourceConfig(name="Keep", type="ALSA", config={"device":"hw:2,0"}))
            data = source.model_dump(mode="json", exclude={"id"})
            expected[source.id] = data.copy()
            if flag is not None: data["enabled"] = flag
            db.connection.execute("UPDATE sources SET data=? WHERE id=?", (json.dumps(data), source.id))
        others = {table:list(map(tuple,db.connection.execute("SELECT * FROM " + table)))
                  for table in ("modulators", "audio_profiles", "system_config")}
        db.connection.execute("PRAGMA user_version=7")
    db.close()
    for _ in range(2):
        db = Database(path)
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 9
        for row in db.connection.execute("SELECT id,data FROM sources"):
            assert json.loads(row["data"]) == expected[row["id"]]
        for table, rows in others.items():
            assert list(map(tuple,db.connection.execute("SELECT * FROM " + table))) == rows
        assert len(db.list("sources")) == 3
        db.close()


def test_schema8_rollback(tmp_path):
    path = tmp_path / "bad.db"; db = Database(path)
    with db.connection:
        db.connection.execute("INSERT INTO sources(data) VALUES (?)", (json.dumps({"enabled":False}),))
        db.connection.execute("INSERT INTO sources(data) VALUES ('invalid')")
        db.connection.execute("PRAGMA user_version=7")
        before = list(map(tuple,db.connection.execute("SELECT * FROM sources")))
    db.close()
    with pytest.raises(json.JSONDecodeError): Database(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("SELECT * FROM sources").fetchall() == before


def test_source_contract_and_shared_demand(tmp_path):
    app = create_app(Settings(tmp_path / "api.db"))
    with TestClient(app) as api:
        root = "/api/v1"
        body = dict(name="Shared", type="HTTP", config={"uri":"http://example.org/live"})
        assert api.post(root + "/sources", json=body | {"enabled":True}).status_code == 422
        source = api.post(root + "/sources", json=body).json()
        assert "enabled" not in source
        assert api.patch(root + f"/sources/{source['id']}", json={"enabled":False}).status_code == 422
        schema = api.get("/openapi.json").json()["components"]["schemas"]
        for name in ("InputSource", "InputSourceConfig", "InputSourcePatch"):
            assert "enabled" not in schema[name]["properties"]
        services = app.state.services
        for channel in (0,1):
            assert api.patch(root + f"/modulators/{channel}", json={"frequency_khz":98100,"input_source_id":source["id"]}).status_code == 200
            assert api.post(root + f"/modulators/{channel}/start").status_code == 200
        assert len(services.sources.pipelines) == 1
        assert services.sources.consumers[source["id"]] == {0,1}
        assert api.post(root + "/modulators/0/stop").status_code == 200
        assert len(services.sources.pipelines) == 1
        assert api.post(root + "/modulators/1/stop").status_code == 200
        assert not services.sources.pipelines
