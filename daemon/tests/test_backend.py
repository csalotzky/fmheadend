from fmheadendd.tests.profile_support import profile_settings, profile_store
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig, ModulatorPatch, RtOverride
from fmheadendd.modulators.manager import ModulatorManager
from fmheadendd.mux.client import MockMuxClient
from fmheadendd.rds.radiotext import RadioTextService
from fmheadendd.rp2354.client import MockRp2354Client
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.pipeline import MockPipeline

P = "/api/v1"
HTTP = {"name": "Radio", "type": "HTTP", "config": {"uri": "https://example.org/live.m3u8"}}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(profile_settings(tmp_path / "headend.db"))) as client:
        yield client


@pytest.mark.parametrize('transport', ['spidev', 'mock'])
def test_mux_transport_requires_observation_and_survives_disconnect(tmp_path, monkeypatch, transport):
    from fmheadendd.mux.unix_client import UnixMuxClient

    mux = UnixMuxClient(lambda unused: None)
    reply = None

    def request(command, **fields):
        assert command == 'get_status'
        if reply is None:
            raise DependencyUnavailable('Mux control unavailable')
        return reply

    monkeypatch.setattr(mux, '_request', request)
    app = create_app(profile_settings(tmp_path / 'headend.db'), mux=mux)
    with TestClient(app) as api:
        initial = api.get(P + '/status').json()['system']
        assert initial['system_state'] == 'DEGRADED'
        assert initial['controller_state'] == 'OK'
        assert initial['mux_state'] == 'ERROR'
        assert initial['mux_transport'] is None
        assert initial['mux_mocked'] is None

        observed = MockMuxClient()
        observed.set_tx_count(8)
        status = observed.get_status()
        status.update(transport=transport, mocked=transport == 'mock')
        for channel in status['channels']:
            channel.update(bytes_received=0, bytes_consumed=0)

        def refresh(value):
            nonlocal reply
            reply = value
            app.state.services.status.refresh()

        app.state.control.executor.submit(refresh, status).result(timeout=3)
        connected = api.get(P + '/status').json()['system']
        assert connected['mux_state'] == 'RUNNING'
        assert connected['mux_transport'] == transport
        assert connected['mux_mocked'] is (transport == 'mock')
        app.state.control.executor.submit(refresh, None).result(timeout=3)
        disconnected = api.get(P + '/status').json()['system']
        assert disconnected['system_state'] == 'DEGRADED'
        assert disconnected['mux_state'] == 'ERROR'
        assert disconnected['mux_transport'] == transport
        assert disconnected['mux_mocked'] is (transport == 'mock')


def source(client, body=None):
    response = client.post(P + "/sources", json=body or HTTP)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def assign(client, tx, source_id, **kwargs):
    response = client.patch(f"{P}/modulators/{tx}", json={"input_source_id": source_id, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('field,value', [
    ('spi_device', '/dev/spidev1.0'), ('spi_clock_hz', 1000000),
    ('i2c_device', '/dev/i2c-3'), ('controller_i2c_address', 98),
    ('web_listen_address', '127.0.0.1'), ('web_port', 8888),
    ('controller_status_poll_interval_ms', 500), ('log_level', 'DEBUG'),
])
def test_system_config_rejects_startup_and_mux_owned_settings(client, field, value):
    before = client.get(P + '/system/config').json()
    assert set(before) == {'default_audio_profile_id', 'global_stopped'}
    response = client.patch(P + '/system/config', json={field: value, 'default_audio_profile_id': 'default/bypass.json'})
    assert response.status_code == 422
    assert client.get(P + '/system/config').json() == before
    schemas = client.get('/openapi.json').json()['components']['schemas']
    for name in ('SystemConfig', 'SystemConfigPatch', 'SystemSaveResult'):
        assert field not in schemas[name]['properties']
    response = client.patch(P + '/system/config', json={'default_audio_profile_id': 'default/bypass.json'})
    assert response.status_code == 200
    assert {'spi_device', 'spi_clock_hz'}.isdisjoint(response.json())
    assert client.get(P + '/system/config').json() == {**before, 'default_audio_profile_id': 'default/bypass.json'}


def test_docs_and_fixed_modulators(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    for path in ("/sources/test", "/sources/{id}/test", "/audio-profiles/files", "/modulators/{id}/rds/rt", "/status"):
        assert P + path in schema["paths"]
    assert schema["paths"][P + "/sources"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")
    rows = client.get(P + "/modulators").json()
    assert [r["id"] for r in rows] == list(range(8))
    assert all(not r["enabled"] and r["frequency_khz"] is None for r in rows)
    assert client.post(P + "/modulators", json={}).status_code == 405
    assert client.delete(P + "/modulators/0").status_code == 405
    assert client.get(P + "/modulators/8").status_code == 404


@pytest.mark.parametrize('failure', ['source', 'mux', 'both'])
def test_stop_http_success_survives_cleanup_failure(client, monkeypatch, failure):
    sid = source(client)
    assign(client, 0, sid, frequency_khz=98100)
    assert client.post(P + '/modulators/0/start').status_code == 200
    s = client.app.state.services
    pipeline = s.sources.pipelines[sid]
    stop, detach = pipeline.stop, s.mux.detach
    def fail(*args):
        raise DependencyUnavailable('teardown timed out')
    if failure in ('source', 'both'): monkeypatch.setattr(pipeline, 'stop', fail)
    if failure in ('mux', 'both'): monkeypatch.setattr(s.mux, 'detach', fail)
    response = client.post(P + '/modulators/0/stop')
    assert response.status_code == 200
    assert response.json()['tx_state'] == 'OFF'
    assert 'RF OFF confirmed; cleanup pending' in response.json()['last_error']
    assert not s.rp.enabled[0] and not client.get(P + '/modulators/0').json()['enabled']
    assert (0 in s.modulators.active_sources) == (failure in ('source', 'both'))
    assert client.get(P + '/status').json()['system']['system_state'] == 'DEGRADED'
    monkeypatch.setattr(pipeline, 'stop', stop)
    monkeypatch.setattr(s.mux, 'detach', detach)
    retried = client.post(P + '/modulators/0/stop')
    assert retried.status_code == 200 and retried.json()['last_error'] is None
    assert 0 not in s.modulators.active_sources


def test_crud_constraints_and_probe_isolation(client):
    before = client.get(P + "/modulators").json()
    probe = client.post(P + "/sources/test", json=HTTP)
    assert probe.status_code == 200
    assert probe.json()["mocked"] is True
    assert client.get(P + "/sources").json() == []
    sid = source(client)
    saved = client.get(f"{P}/sources/{sid}").json()
    assert client.post(f"{P}/sources/{sid}/test").json() == probe.json()
    assert client.get(f"{P}/sources/{sid}").json() == saved
    assert client.get(P + "/modulators").json() == before
    assert client.patch(f"{P}/sources/{sid}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    assign(client, 0, sid)
    assert client.delete(f"{P}/sources/{sid}").status_code == 409
    assign(client, 0, None)
    assert client.delete(f"{P}/sources/{sid}").status_code == 204
    assert client.get(f"{P}/sources/{sid}").status_code == 404


@pytest.mark.parametrize("kind,config", [
    ("HTTP", {"uri": "https://example.org/stream"}),
    ("UDP", {"port": 5000, "format": "S16LE", "sample_rate": 48000, "channels": 2}),
    ("RTP", {"port": 5004, "codec": "OPUS", "payload_type": 96}),
    ("ALSA", {"device": "hw:1,0"}),
])
def test_source_types(client, kind, config):
    sid = source(client, {"name": kind, "type": kind, "config": config})
    assert client.post(f"{P}/sources/{sid}/test").json()["ok"]


@pytest.mark.parametrize("body", [
    {"type": "HTTP", "config": {}},
    {"type": "HTTP", "config": {"uri": "file:///tmp/a"}},
    {"type": "UDP", "config": {"port": 5000}},
    {"type": "RTP", "config": {"port": 70000, "codec": "OPUS", "payload_type": 96}},
    {"type": "ALSA", "config": {}},
    {"type": "ANALOG", "config": {"uri": "ignored"}},
    {"type": "HLS", "config": {"uri": "https://example.org/a"}},
])
def test_invalid_sources(client, body):
    response = client.post(P + "/sources", json={"name": "Bad", **body})
    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}
    assert client.get(P + "/sources").json() == []


@pytest.mark.parametrize("patch", [
    {"frequency_khz": 75950}, {"frequency_khz": 98000.5}, {"preemphasis_us": 25},
    {"rds_pi": 65536}, {"rds_pty": 32}, {"rds_ps": []},
    {"rds_ps": [{"text": "TOO LONG!", "duration_s": 5}]},
    {"rds_ps": [{"text": "ABC", "duration_s": 0}]},
    {"rds_rt": "x" * 65}, {"rds_rt": "a\nb"}, {"name": None}, {"id": 3},
])
def test_invalid_modulator_patch_is_atomic(client, patch):
    before = client.get(P + "/modulators/0").json()
    response = client.patch(P + "/modulators/0", json=patch)
    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error", "message"}
    assert client.get(P + "/modulators/0").json() == before


def test_patch_revalidates_source_type(client):
    sid = source(client)
    assert client.patch(f"{P}/sources/{sid}", json={"type": "ANALOG"}).status_code == 422
    assert client.patch(f"{P}/sources/{sid}", json={"type": "ALSA", "config": {"device": "hw:1,0"}}).status_code == 200


def test_old_profile_api_removed(client):
    assert client.get(P + '/audio-profiles').status_code == 404
    assert client.post(P + '/sources', json=HTTP | {'audio_profile_id': 9}).status_code == 422


def test_start_stop_status_active_edit_and_bulk(client):
    assert client.post(P + "/modulators/0/start").status_code == 409
    sid = source(client)
    assign(client, 0, sid, frequency_khz=98100)
    assert client.post(P + "/modulators/0/start").json()["tx_state"] == "ON"
    assert client.get(P + "/modulators/0").json()["enabled"]
    assert client.get(f"{P}/sources/{sid}/status").json()["state"] == "RUNNING"
    result = client.patch(f"{P}/sources/{sid}", json={"name": "Busy"})
    assert result.status_code == 200 and result.json()['applied']
    status = client.get(P + "/status").json()
    assert status["modulators"][0]["tx_state"] == "ON"
    assert status["modulators"][0]["rf_attenuation_db"] is None
    assert "text" not in status["modulators"][0]
    assert client.patch(P + "/modulators/0", json={"frequency_khz": 99500}).status_code == 409
    assert client.post(P + "/modulators/0/stop").json()["tx_state"] == "OFF"
    assert client.get(f"{P}/sources/{sid}/status").json()["state"] == "IDLE"
    assert len(client.post(P + "/modulators/start-all").json()) == 8
    assert all(r["tx_state"] == "OFF" for r in client.post(P + "/modulators/stop-all").json())


def test_restart_persistence_and_runtime_reset(tmp_path):
    settings = profile_settings(tmp_path / "persistent.db")
    with TestClient(create_app(settings)) as client:
        sid = source(client)
        assign(client, 3, sid, frequency_khz=98100, rds_rt="Fallback")
        client.post(P + "/modulators/3/start")
        client.put(P + "/modulators/3/rds/rt", json={"text": "Override"})
        assert client.patch(P + "/system/config", json={"default_audio_profile_id": "default/bypass.json"}).status_code == 200
    with TestClient(create_app(settings)) as client:
        row = client.get(P + "/modulators/3").json()
        assert row["frequency_khz"] == 98100 and row["enabled"]
        assert row["input_source_id"] == sid
        assert client.get(P + "/modulators/3/rds/rt").json()["text"] == "Fallback"
        assert client.get(P + "/status").json()["modulators"][3]["tx_state"] == "ON"
        assert client.get(P + "/system/config").json()["default_audio_profile_id"] == "default/bypass.json"
        assert client.get(f"{P}/sources/{sid}").json()["name"] == HTTP["name"]


def test_rt_ttl_api(client):
    assert client.put(P + "/modulators/0/rds/rt", json={"text": "Hello", "ttl_s": 0}).status_code == 422
    assert client.put(P + "/modulators/0/rds/rt", json={"text": "Hello"}).json()["origin"] == "override"
    assert client.delete(P + "/modulators/0/rds/rt").status_code == 204
    assert client.get(P + "/modulators/0/rds/rt").json()["origin"] == "fallback"


def test_uniform_http_and_internal_errors(client):
    assert set(client.get("/missing").json()) == {"error", "message"}
    assert set(client.get(P + "/modulators/not-an-int").json()) == {"error", "message"}
    response = client.post(P + "/sources", content="{", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}


def test_concurrent_requests_use_single_event_loop_connection(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda i: client.post(P + "/sources", json=HTTP | {"name": str(i)}), range(12)))
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 12


@pytest.fixture
def services(tmp_path):
    db = Database(tmp_path / "unit.db")
    rp, mux = MockRp2354Client(), MockMuxClient()
    sources = SourceManager(db, profile_files=profile_store(tmp_path))
    manager = ModulatorManager(db, sources, rp, mux, Settings())
    from fmheadendd.recovery import Recovery
    from unittest.mock import Mock
    manager.recovery = Recovery(manager, Mock())
    manager.recovery.observe()
    try:
        yield db, rp, mux, sources, manager
    finally:
        db.close()


def setup_tx(services, tx=0, analog=False):
    db, _, _, _, manager = services
    if analog:
        manager.update(tx, ModulatorPatch(audio_mode="ANALOG", frequency_khz=98100))
        return None
    source = db.save("sources", InputSourceConfig.model_validate(HTTP))
    manager.update(tx, ModulatorPatch(input_source_id=source.id, frequency_khz=98100))
    return source.id


def test_shared_pipeline_and_analog_bypass(services):
    db, rp, mux, sources, manager = services
    sid = setup_tx(services)
    manager.update(1, ModulatorPatch(input_source_id=sid, frequency_khz=98100))
    manager.start(0)
    pipeline = sources.pipelines[sid]
    manager.start(1)
    manager.start(1)
    assert sources.pipelines[sid] is pipeline
    assert sources.consumers[sid] == {0, 1}
    manager.stop(0)
    assert pipeline.running and mux.producers == {1: sid}
    manager.stop(1)
    assert not pipeline.running and not sources.pipelines
    setup_tx(services, tx=2, analog=True)
    manager.start(2)
    assert rp.configs[2].audio_mode == "ANALOG"
    assert not sources.pipelines and not mux.producers


def test_lifecycle_order(services):
    db, rp, mux, sources, manager = services
    sid = setup_tx(services)
    events = []
    class Pipeline(MockPipeline):
        def start(self):
            events.append("pipeline start")
            super().start()
        def stop(self):
            events.append("pipeline stop")
            super().stop()
    sources.pipeline_factory = Pipeline
    original_config, original_enable = rp.set_tx_config, rp.set_tx_enabled
    def config(tx, value):
        events.append("config " + value.audio_mode)
        original_config(tx, value)
    def enable(tx, value):
        events.append("RF " + str(value))
        original_enable(tx, value)
    rp.set_tx_config, rp.set_tx_enabled = config, enable
    manager.start(0)
    manager.stop(0)
    assert events == ["pipeline start", "config DIGITAL", "RF True", "RF False", "pipeline stop"]


def test_rt_priority_and_exact_expiry(services):
    db, rp, mux, sources, manager = services
    sid = setup_tx(services)
    manager.update(0, ModulatorPatch(rds_rt="Fallback", rds_metadata_to_rt=True))
    now = [100.0]
    rt = RadioTextService(db, sources, lambda: now[0])
    assert rt.effective(0).text == "Fallback"
    sources.status(sid).stream_title = "Metadata"
    assert rt.effective(0).text == "Metadata"
    rt.set(0, RtOverride(text="Override", ttl_s=10))
    now[0] = 109.99
    assert rt.effective(0).text == "Override"
    now[0] = 110
    assert rt.effective(0).text == "Metadata" and not rt.overrides
    rt.set(0, RtOverride(text=""))
    now[0] += 100000
    assert rt.effective(0).text == ""
    rt.clear(0)
    manager.update(0, ModulatorPatch(rds_metadata_to_rt=False))
    assert rt.effective(0).text == "Fallback"
    assert db.get("modulators", 0).rds_rt == "Fallback"


def test_active_edit_rejection_preserves_config_and_audio_without_rp_access(services):
    db, rp, mux, sources, manager = services
    sid = setup_tx(services)
    manager.start(0)
    rp.available = False
    with pytest.raises(Exception) as error:
        manager.update(0, ModulatorPatch(frequency_khz=99900))
    assert error.value.status == 409
    assert db.get("modulators", 0).frequency_khz == 98100
    assert manager.statuses[0].tx_state == "ON"
    assert sources.pipelines[sid].running
    rp.available = True
    manager.stop(0)
    assert not sources.pipelines


def test_pipeline_failure_never_enables_rf(services):
    db, rp, mux, sources, manager = services
    setup_tx(services)
    class BrokenPipeline(MockPipeline):
        def start(self):
            raise DependencyUnavailable("Source failed")
    sources.pipeline_factory = BrokenPipeline
    with pytest.raises(Exception) as error:
        manager.start(0)
    assert error.value.status == 503
    assert not any(rp.enabled) and not mux.producers
    assert manager.statuses[0].tx_state == "ERROR"


def test_status_reads_cache_without_hardware_calls(client):
    status = client.app.state.services.status
    def forbidden():
        raise AssertionError("HTTP status must not call hardware")
    # snapshot itself is pure with respect to hardware; maintenance uses refresh.
    old_rp, old_mux = status.rp, status.mux
    status.rp = status.mux = type("NoIO", (), {"get_status": staticmethod(forbidden)})()
    try:
        assert status.snapshot().system.mocked
    finally:
        status.rp, status.mux = old_rp, old_mux


def test_schema_version_and_fixed_rows(tmp_path):
    path = tmp_path / "schema.db"
    db = Database(path)
    assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute("DELETE FROM modulators WHERE id=0")
    db.connection.rollback()
    db.close()
    db = Database(path)
    assert len(db.list("modulators")) == 8
    db.connection.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(RuntimeError, match="Unsupported database schema 99"):
        Database(path)


def test_mux_failure_releases_source_even_when_rp_unavailable(services):
    db, rp, mux, sources, manager = services
    setup_tx(services)
    mux.available = rp.available = False
    with pytest.raises(Exception) as error:
        manager.start(0)
    assert error.value.status == 503
    assert not sources.pipelines and not mux.producers and not manager.active_sources
    assert db.get("modulators", 0).enabled


def test_failed_enable_retains_tracked_audio_until_successful_stop(services):
    db, rp, mux, sources, manager = services
    sid = setup_tx(services)
    original = rp.set_tx_enabled
    def fail_enable(tx, enabled):
        if enabled:
            rp.available = False
        original(tx, enabled)
    rp.set_tx_enabled = fail_enable
    with pytest.raises(Exception) as error:
        manager.start(0)
    assert error.value.status == 503
    assert manager.active_sources == {0: sid}
    assert sources.pipelines[sid].running
    rp.available = True
    manager.stop(0)
    assert not sources.pipelines and not manager.active_sources


def test_dependency_errors_through_api(tmp_path):
    rp = MockRp2354Client()
    app = create_app(profile_settings(tmp_path / "errors.db"), rp=rp)
    with TestClient(app) as client:
        sid = source(client)
        assign(client, 0, sid, frequency_khz=98100)
        rp.available = False
        response = client.post(P + "/modulators/0/start")
        assert response.status_code == 503
        assert set(response.json()) == {"error", "message"}
        assert client.get(P + "/modulators/0").json()["enabled"]
        status = client.get(P + "/status").json()
        assert status["system"]["system_state"] == "DEGRADED"
        assert status["modulators"][0]["tx_state"] == "OFF"
        assert status["system"]["controller_observation_stale"]
        rp.available = True


def test_internal_error_is_sanitized(tmp_path):
    app = create_app(profile_settings(tmp_path / "internal.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        def fail(table, record_id):
            raise RuntimeError("private internal details")
        app.state.services.config.get = fail
        response = client.get(P + "/sources/1")
        assert response.status_code == 500
        assert response.json() == {"error": "INTERNAL_ERROR", "message": "Internal server error"}


def test_cli_serves_http(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "daemon.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "fmheadendd.app", "--mock", "--database", str(tmp_path / "cli.db"),
             "--host", "127.0.0.1", "--port", str(port)],
            stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/status", timeout=1) as response:
                        assert response.status == 200
                        assert b'"mocked":true' in response.read()
                    break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail("CLI did not start: " + log.read())
                    time.sleep(0.1)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=1) as response:
                assert response.status == 200
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_unconfigured_frequency_blocks_enable_without_side_effects(tmp_path):
    rp, mux = MockRp2354Client(), MockMuxClient()
    settings = profile_settings(tmp_path / "unset.db")
    app = create_app(settings, rp=rp, mux=mux)
    with TestClient(app) as client:
        sid = source(client)
        assign(client, 0, sid)
        before = client.get(P + "/modulators/0").json()
        assert before["frequency_khz"] is None
        for method, path, body in (
            ("POST", "/modulators/0/start", None),
            ("PATCH", "/modulators/0", {"enabled": True}),
        ):
            response = client.request(method, P + path, json=body)
            assert response.status_code == 409
            assert response.json()["error"] == "FREQUENCY_REQUIRED"
            assert client.get(P + "/modulators/0").json() == before
            assert not any(rp.enabled) and not rp.configs and not mux.producers
            assert client.get(f"{P}/sources/{sid}/status").json()["state"] == "IDLE"
        results = client.post(P + "/modulators/start-all").json()
        assert all(row["tx_state"] == "OFF" for row in results)
        assert not any(rp.enabled)
    with TestClient(create_app(settings)) as client:
        assert all(row["frequency_khz"] is None and not row["enabled"]
                   for row in client.get(P + "/modulators").json())


def test_frequency_must_be_explicit_and_can_be_cleared_while_disabled(client):
    sid = source(client)
    assign(client, 0, sid, frequency_khz=98100)
    assert client.post(P + "/modulators/0/start").status_code == 200
    response = client.patch(P + "/modulators/0", json={"frequency_khz": None})
    assert response.status_code == 409
    assert client.get(P + "/modulators/0").json()["frequency_khz"] == 98100
    assert client.post(P + "/modulators/0/stop").status_code == 200
    assert client.patch(P + "/modulators/0", json={"frequency_khz": None}).json()["frequency_khz"] is None
    assert client.post(P + "/modulators/0/start").json()["error"] == "FREQUENCY_REQUIRED"
