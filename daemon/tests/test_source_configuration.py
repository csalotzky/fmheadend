"""Demand-driven source configuration."""
from fmheadendd.tests.profile_support import profile_settings
from fastapi.testclient import TestClient
from fmheadendd.app import create_app


def test_source_contract_and_shared_demand(tmp_path):
    app = create_app(profile_settings(tmp_path / "api.db"))
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
