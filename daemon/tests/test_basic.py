"""Typed DSP settings and native property mapping; no hardware required."""
import pytest
from pydantic import ValidationError
from fmheadendd.models.configuration import AudioProfileConfig, BasicConfig
from fmheadendd.database import Database
from fmheadendd.sources.basic import basic_stages


def test_defaults_and_persistence(tmp_path):
    path = tmp_path / "profiles.db"
    db = Database(path)
    profile = db.save("audio_profiles", AudioProfileConfig(name="Basic", processor="GST_BASIC", gain_db=-3))
    assert profile.basic_config.model_dump() == dict(highpass_enabled=True,highpass_hz=40,
        eq_low_db=0,eq_mid_db=0,eq_high_db=0,compressor_enabled=True,
        compressor_threshold_dbfs=-12,compressor_ratio=2,lowpass_enabled=True,
        lowpass_hz=15000,limiter_enabled=True)
    custom = profile.model_copy(update={"basic_config": BasicConfig(highpass_hz=80,eq_mid_db=-2.5,compressor_ratio=4)})
    db.save("audio_profiles",custom,profile.id)
    db.close()
    db = Database(path)
    assert db.get("audio_profiles",profile.id).basic_config == custom.basic_config
    assert db.get("audio_profiles",profile.id).gain_db == -3
    db.close()

@pytest.mark.parametrize("field,value", [
    ("highpass_hz",41),("lowpass_hz",14500),("eq_low_db",-24.5),
    ("eq_mid_db",12.5),("eq_high_db",0.1),("compressor_ratio",2.5),
    ("compressor_threshold_dbfs",-31),("compressor_threshold_dbfs",-2),
    ("compressor_threshold_dbfs",-12.5), ("eq_low_db",float("nan"))])
def test_invalid_settings(field,value):
    with pytest.raises(ValidationError): BasicConfig(**{field:value})


def test_order_and_all_eq_bands():
    stages=basic_stages(lambda name,**props:(name,props),BasicConfig(eq_low_db=-24,eq_mid_db=1.5,eq_high_db=12))
    assert [n for n,p in stages] == ['audiocheblimit','equalizer-3bands','audiodynamic','audiocheblimit','rglimiter']
    assert stages[0][1] == dict(mode=1,type=1,cutoff=40,ripple=0,poles=4)
    assert stages[1][1] == dict(band0=-24,band1=1.5,band2=12)
    assert stages[3][1] == dict(mode=0,type=1,cutoff=15000,ripple=0,poles=4)
    assert stages[4][1] == dict(enabled=True)

@pytest.mark.parametrize('ratio',[1,1.5,2,3,4])
def test_compressor_mapping(ratio):
    stages=basic_stages(lambda name,**props:(name,props),BasicConfig(compressor_ratio=ratio,compressor_threshold_dbfs=-20))
    props=stages[2][1]
    assert props == dict(mode=0,characteristics=1,threshold=0.1,ratio=1/ratio)

@pytest.mark.parametrize('flag,factory', [('highpass_enabled','audiocheblimit'),('compressor_enabled','audiodynamic'),('lowpass_enabled','audiocheblimit'),('limiter_enabled','rglimiter')])
def test_stage_bypass(flag,factory):
    stages=basic_stages(lambda name,**props:name,BasicConfig(**{flag:False}))
    assert stages.count(factory) == (1 if factory=='audiocheblimit' else 0)

@pytest.mark.parametrize('cutoff',[20,30,40,50,60,80,100])
def test_highpass_cutoffs(cutoff):
    assert basic_stages(lambda name,**props:props,BasicConfig(highpass_hz=cutoff))[0]['cutoff']==cutoff

@pytest.mark.parametrize('cutoff',[12000,13000,14000,15000,16000])
def test_lowpass_cutoffs(cutoff):
    assert basic_stages(lambda name,**props:props,BasicConfig(lowpass_hz=cutoff))[3]['cutoff']==cutoff
