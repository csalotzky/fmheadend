"""Current protocol audio controls: exact wire vectors and backend integration, no RP hardware."""
import json
import sqlite3
import struct
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import ModulatorConfig, InputSourceConfig, AudioProfileConfig
from fmheadendd.rp2354.client import TxConfig, MockRp2354Client
from fmheadendd.rp2354 import protocol as p

DEFAULTS = dict(gain_mode="AUTO", analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False)
MANUAL = dict(gain_mode="MANUAL", analog_gain_db=-1.5, digital_trim_db=1, soft_clipping=True)


def config(**changes):
    return TxConfig(frequency_khz=98100, rf_attenuation_db=6.0, stereo=True,
                    preemphasis_us=50, audio_mode="ANALOG", rds_enabled=True, **changes)


@pytest.mark.parametrize("mode,code", [("AUTO", 0), ("MANUAL", 1)])
@pytest.mark.parametrize("gain,encoded", [(0.0, "0000"), (6.0, "3c00"), (-1.5, "f1ff"),
                                         (-3276.8, "0080"), (3276.7, "ff7f")])
def test_exact_config(mode, code, gain, encoded):
    body = p.tx_config(config(gain_mode=mode, analog_gain_db=gain, digital_trim_db=1, soft_clipping=True))
    expected = bytes.fromhex("347f01003c0001320101") + bytes([code]) + bytes.fromhex(encoded) + b"\x01\x01"
    assert body == expected and len(body) == 15
    assert p.request(p.Command.SET_TX_CONFIG, 7, body) == b"\x02\x07\x0f" + expected
    assert struct.unpack("<IHBBBBBhBB", body) == (98100, 60, 1, 50, 1, 1, code, int(gain * 10), 1, 1)


@pytest.mark.parametrize("field,value", [("gain_mode", "invalid"), ("gain_mode", 0),
    ("analog_gain_db", 0.15), ("analog_gain_db", 3276.8), ("analog_gain_db", -3276.9),
    ("analog_gain_db", float("nan")), ("analog_gain_db", True),
    ("digital_trim_db", 2), ("digital_trim_db", -1), ("digital_trim_db", True),
    ("digital_trim_db", 0.5), ("soft_clipping", 1)])
def test_validation_at_model_and_wire_boundary(field, value):
    with pytest.raises(ValidationError):
        ModulatorConfig(**{field: value})
    with pytest.raises(ValidationError):
        config(**{field: value})
    with pytest.raises(ValueError):
        p.tx_config(config().model_copy(update={field: value}))


@pytest.mark.parametrize("offset", [10, 13, 14])
def test_raw_config_invalid_enums(offset):
    body = bytearray(p.tx_config(config()))
    body[offset] = 2
    with pytest.raises(ValueError):
        p.request(p.Command.SET_TX_CONFIG, 0, bytes(body))


@pytest.mark.parametrize("size", [10, 14, 16])
def test_config_wrong_lengths(size):
    with pytest.raises(ValueError):
        p.request(p.Command.SET_TX_CONFIG, 0, bytes(size))


def status_payload():
    prefix = bytes.fromhex("00010000000200000000")
    return prefix + b"".join(bytes.fromhex("013c000300000004000000200000") + bytes([sat])
                             for sat in (0, 1, 255, 0, 1, 255, 0, 1))


def test_status_exact_layout_and_saturation():
    payload = status_payload()
    assert len(payload) == 130
    assert p.response(p.Command.GET_STATUS, b"\x00\x82" + payload) == payload
    status = p.parse_status(payload)
    assert status["uptime_ms"] == 1 and status["rp_spi_audio_link_error_count"] == 2
    for index, expected in enumerate((False, True, None, False, True, None, False, True)):
        assert status["modulators"][index] == dict(id=index, tx_state="ON", rf_attenuation_db=6.0,
            audio_underrun_count=3, rds_underrun_count=4, rds_fifo_free=32,
            last_error_code=None, input_saturated=expected)


@pytest.mark.parametrize("size", [122, 129, 131])
def test_status_wrong_lengths(size):
    with pytest.raises(p.ProtocolError): p.parse_status(bytes(size))


@pytest.mark.parametrize("value", [2, 254])
def test_status_invalid_saturation(value):
    data = bytearray(status_payload()); data[24] = value
    with pytest.raises(p.ProtocolError, match="input_saturated"): p.parse_status(data)


def test_schema6_preservation_and_reopen(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    profile = db.save("audio_profiles", AudioProfileConfig(name="Keep", gain_db=-3))
    source = db.save("sources", InputSourceConfig(name="Keep", type="ALSA", config={"device":"hw:2,0"}, audio_profile_id=profile.id))
    before = {}
    with db.connection:
        for row in db.connection.execute("SELECT id,data FROM modulators").fetchall():
            data = json.loads(row["data"])
            for key in DEFAULTS: data.pop(key)
            data.update(frequency_khz=98100 + row["id"] * 100, rds_enabled=True, rds_ecc=224,
                        rds_rt="Keep text", enabled=row["id"] == 0,
                        audio_mode="ANALOG" if row["id"] < 2 else "DIGITAL",
                        input_source_id=None if row["id"] < 2 else source.id)
            before[row["id"]] = data
            db.connection.execute("UPDATE modulators SET data=?,input_source_id=? WHERE id=?",
                                  (json.dumps(data),data["input_source_id"],row["id"]))
        other = {table:list(map(tuple, db.connection.execute("SELECT * FROM " + table)))
                 for table in ("sources", "audio_profiles", "system_config")}
        db.connection.execute("PRAGMA user_version=6")
    db.close()
    for _ in range(2):
        db = Database(path)
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 9
        for row in db.connection.execute("SELECT id,data FROM modulators"):
            assert json.loads(row["data"]) == before[row["id"]] | DEFAULTS
        for table, rows in other.items():
            assert list(map(tuple,db.connection.execute("SELECT * FROM " + table))) == rows
        assert len(db.list("modulators")) == 8
        db.close()


def test_schema7_failure_is_atomic(tmp_path):
    path = tmp_path / "bad.db"; db = Database(path)
    with db.connection:
        db.connection.execute("UPDATE modulators SET data='invalid' WHERE id=7")
        db.connection.execute("PRAGMA user_version=6")
        before = list(map(tuple,db.connection.execute("SELECT * FROM modulators")))
    db.close()
    with pytest.raises(json.JSONDecodeError): Database(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT * FROM modulators").fetchall() == before


@pytest.mark.parametrize("settings", [DEFAULTS, MANUAL])
def test_api_recovery_replays_full_config_and_current_saturation(tmp_path, settings):
    now = [0.0]; rp = MockRp2354Client(clock=lambda: now[0])
    app = create_app(Settings(tmp_path / "api.db"), rp=rp, clock=lambda: now[0])
    def refresh(): app.state.control.executor.submit(app.state.services.status.refresh).result()
    with TestClient(app) as api:
        prefix = "/api/v1"
        assert api.get(prefix + "/system/info").json()["rp_protocol_version"] == 1
        original = api.get(prefix + "/modulators/0").json()
        assert {key:original[key] for key in DEFAULTS} == DEFAULTS
        body = dict(audio_mode="ANALOG", frequency_khz=98100, rf_attenuation_db=6.0, rds_enabled=True, **settings)
        result = api.patch(prefix + "/modulators/0", json=body)
        assert result.status_code == 200
        assert {key:result.json()[key] for key in settings} == settings
        assert not rp.enabled[0]  # Configuration alone never enables RF.
        assert api.post(prefix + "/modulators/0/start").status_code == 200
        for saturation in (False, True, False):
            rp.input_saturated[0] = saturation; refresh()
            assert api.get(prefix + "/status").json()["modulators"][0]["input_saturated"] is saturation
        rp.available = False; refresh()
        assert api.get(prefix + "/status").json()["modulators"][0]["input_saturated"] is None
        rp.configs.clear(); rp.available = True; now[0] = 3; refresh()
        assert {key:getattr(rp.configs[0],key) for key in settings} == settings
        assert rp.enabled[0] and app.state.services.recovery.ready
        assert not app.state.services.sources.pipelines
        assert api.post(prefix + "/modulators/0/stop").status_code == 200
        assert api.patch(prefix + "/modulators/0", json={"audio_mode":"DIGITAL"}).status_code == 200
        assert api.get(prefix + "/status").json()["modulators"][0]["input_saturated"] is None
        rp.input_saturated[0] = True; refresh()
        assert api.get(prefix + "/status").json()["modulators"][0]["input_saturated"] is None
        assert api.post(prefix + "/modulators/0/start").status_code == 409


def test_mock_digital_saturation_unavailable():
    rp = MockRp2354Client(); rp.input_saturated[0] = True
    rp.set_tx_config(0,config().model_copy(update={"audio_mode":"DIGITAL"}))
    assert rp.get_status()["modulators"][0]["input_saturated"] is None
    assert not rp.enabled[0]
