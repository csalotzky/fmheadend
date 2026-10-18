import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app, argument_parser
from fmheadendd.config import Settings
from fmheadendd.regions import PLANS


def test_default_and_invalid_cli():
    assert argument_parser().parse_args([]).region==1
    for args in (['--region','9'],['--fm-band-min-khz','87500'],['--fm-band-max-khz','108000']):
        with pytest.raises(SystemExit):argument_parser().parse_args(args)
    with pytest.raises(ValueError):Settings(region=9)

@pytest.mark.parametrize('region,minimum,maximum,step,count',[(0,76000,108000,50,641),(1,87500,108000,100,206),(2,88100,107900,200,100),(3,76100,98900,100,229)])
def test_exact_plans_and_info(tmp_path,region,minimum,maximum,step,count):
    assert argument_parser().parse_args(['--region',str(region)]).region==region
    plan=PLANS[region]
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
