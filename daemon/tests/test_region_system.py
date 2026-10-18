import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app, argument_parser
from fmheadendd.config import Settings
from fmheadendd.regions import PLANS, FrequencyPlan, effective_plan
from fmheadendd.rp2354.client import MockRp2354Client


def test_default_and_invalid_cli():
    assert argument_parser().parse_args([]).region==1
    for args in (['--region','9'],['--fm-band-min-khz','87500'],['--fm-band-max-khz','108000']):
        with pytest.raises(SystemExit):argument_parser().parse_args(args)
    with pytest.raises(ValueError):Settings(region=9)

@pytest.mark.parametrize('region,minimum,maximum,step,count',[(0,76000,108000,50,641),(1,87500,108000,100,206),(2,88100,107900,200,100),(3,76100,98900,100,229)])
def test_exact_plans_and_info(tmp_path,region,minimum,maximum,step,count):
    assert argument_parser().parse_args(['--region',str(region)]).region==region
    plan=FrequencyPlan(**effective_plan(region, MockRp2354Client().get_info()))
    frequencies=list(plan.frequencies())
    assert len(frequencies)==count
    assert frequencies==[minimum+i*step for i in range(count)]
    assert frequencies[-1]==maximum
    with TestClient(create_app(Settings(tmp_path/'config.db',region))) as api:
        initial_emphasis = 75 if region == 2 else 50
        assert [tx['preemphasis_us'] for tx in api.get('/api/v1/modulators').json()] == [initial_emphasis] * 8
        info=api.get('/api/v1/system/info').json()
        assert info['frequency_plan']==plan.as_dict()
        assert info['database_schema_version']==1 and info['media_backend']=='MOCK'
        assert info['controller_protocol_version']==1
        # Hardware validation is independent of selection region.
        assert api.patch('/api/v1/modulators/0',json={'frequency_khz':76050,'preemphasis_us':75,'rds_af_khz':[87600,107900]}).status_code==200
        assert api.patch('/api/v1/modulators/0',json={'frequency_khz':76025}).status_code==422
    with TestClient(create_app(Settings(tmp_path/'config.db',(region+1)%4))) as api:
        assert [tx['preemphasis_us'] for tx in api.get('/api/v1/modulators').json()][1:] == [initial_emphasis] * 7
        config=api.get('/api/v1/modulators/0').json()
        assert config['frequency_khz']==76050 and config['preemphasis_us']==75
        assert config['rds_af_khz']==[87600,107900]


def test_production_spa_routes_and_api_isolation(tmp_path):
    assets=tmp_path/'browser';assets.mkdir()
    (assets/'index.html').write_text('<html>Production fixture</html>')
    (assets/'main.js').write_text('console.log(1)')
    with TestClient(create_app(Settings(tmp_path/'db',webui_dir=assets))) as api:
        for path in ['/','/sources','/sources/new','/modulators','/modulators/0','/audio-profiles',
                     '/audio-profiles/edit?path=custom%2Fteszt2_20260928180555.json','/system']:
            response=api.get(path)
            assert response.status_code==200 and 'Production fixture' in response.text
        assert api.get('/main.js').status_code==200
        for path in ['/api','/api/unknown','/api/v1/missing','/missing.js','/../private','/bandscan']:
            response=api.get(path)
            assert response.status_code==404 and 'Production fixture' not in response.text
        assert api.get('/api/v1/status').json()['system']['system_state'] in ['RUNNING','DEGRADED']
        assert api.get('/api/v1/modulators/99').status_code==404
        assert api.post('/api/v1/missing').status_code in (404,405)


def test_missing_assets_fail_clearly(tmp_path):
    with pytest.raises(ValueError,match='index.html'):
        create_app(Settings(tmp_path/'db',webui_dir=tmp_path/'missing'))


@pytest.mark.parametrize('region', range(4))
def test_reported_limits_drive_validation_and_only_expert_plan(tmp_path, region):
    from unittest.mock import Mock
    from fmheadendd.database import Database
    from fmheadendd.models.configuration import ModulatorConfig
    path = tmp_path / 'capabilities.db'
    db = Database(path)
    db.save('modulators', ModulatorConfig(frequency_khz=65025, audio_mode='ANALOG'), 0)
    db.close()
    rp = MockRp2354Client()
    reported = rp.get_info() | dict(frequency_min_khz=65025, frequency_max_khz=75025, frequency_step_khz=100)
    rp.get_info = Mock(return_value=reported)
    with TestClient(create_app(Settings(path, region), rp=rp)) as api:
        info = api.get('/api/v1/system/info').json()
        assert info['frequency_min_khz'] == 65025
        assert info['frequency_max_khz'] == 75025
        assert info['frequency_step_khz'] == 100
        assert info['frequency_plan'] == effective_plan(region, reported)
        assert rp.configs[0].frequency_khz == 65025  # Startup uses negotiated capabilities.
        assert api.patch('/api/v1/modulators/0', json={'frequency_khz':75025}).status_code == 200
        assert api.patch('/api/v1/modulators/0', json={'frequency_khz':75000}).status_code == 422
        assert api.get('/api/v1/modulators/0').json()['frequency_khz'] == 75025


def test_unknown_expert_capabilities_are_not_invented(tmp_path):
    rp = MockRp2354Client()
    rp.available = False
    with TestClient(create_app(Settings(tmp_path/'db', 0), rp=rp)) as api:
        info = api.get('/api/v1/system/info').json()
        assert info['frequency_min_khz'] is None
        assert info['frequency_plan']['fm_min_khz'] is None
        assert info['frequency_plan']['fm_step_khz'] is None
