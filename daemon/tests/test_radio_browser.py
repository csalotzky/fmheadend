import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.errors import DomainError
from fmheadendd.radio_browser import RadioBrowserClient, playable_url, discover_servers, MAX_BYTES

UUID_TEXT = '960e57c5-0601-11e8-ae97-52543be04c81'
ROW = dict(stationuuid=UUID_TEXT, name='Example radio', url='https://station.test/live',
    url_resolved='https://cdn.test/token?secret=hidden', country='Austria', countrycode='AT',
    tags=' jazz,rock ', codec='MP3', bitrate=64, hls=0, lastcheckok=1, favicon='https://station.test/icon.png')


async def mirrors():
    return ['one.api.radio-browser.info', 'two.api.radio-browser.info']


def make_client(handler):
    return RadioBrowserClient(transport=httpx.MockTransport(handler), discovery=mirrors)


def test_http_uuid_round_trip_and_independent_unlink(tmp_path):
    with TestClient(create_app(Settings(tmp_path/'rb.db'))) as api:
        body = dict(name='Radio', type='HTTP', config=dict(uri=ROW['url'], radio_browser_uuid=UUID_TEXT))
        created = api.post('/api/v1/sources', json=body)
        assert created.status_code == 201
        sid = created.json()['id']
        path = f'/api/v1/sources/{sid}'
        assert api.get(path).json()['config']['radio_browser_uuid'] == UUID_TEXT
        body['config']['uri'] = 'https://station.test/manually-edited'
        assert api.patch(path, json=body).json()['config']['uri'] == body['config']['uri']
        clear = api.patch(path, json={'config': {'radio_browser_uuid': None}})
        assert clear.status_code == 200
        assert clear.json()['config']['uri'] == body['config']['uri']
        assert clear.json()['config']['radio_browser_uuid'] is None
        assert api.patch(path, json={'config': {'radio_browser_uuid': UUID_TEXT}}).status_code == 200
        for bad in ('bad-id', '', 123):
            assert api.patch(path, json={'config': {'radio_browser_uuid': bad}}).status_code == 422
        assert api.get(path).json()['config']['radio_browser_uuid'] == UUID_TEXT
    with TestClient(create_app(Settings(tmp_path/'rb.db'))) as api:
        assert api.get(path).json()['config']['uri'] == body['config']['uri']
        assert api.get(path).json()['config']['radio_browser_uuid'] == UUID_TEXT
        assert api.delete(path).status_code == 204


def test_directory_endpoints_are_read_only_and_normalized(tmp_path):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.scheme == 'https'
        assert request.headers['user-agent'].startswith('FMheadend/')
        if '/tags' in request.url.path:
            return httpx.Response(200, json=[dict(name='jazz', stationcount='12')])
        if '/countrycodes' in request.url.path:
            return httpx.Response(200, json=[dict(name='AT', stationcount='14')])
        return httpx.Response(200, json=[ROW])
    with TestClient(create_app(Settings(tmp_path/'rb.db'), radio_browser_client=make_client(handler))) as api:
        before = api.get('/api/v1/sources').json()
        result = api.get('/api/v1/radio-browser/stations/search', params=dict(name='Radio', countrycode='at', tag='jazz', offset=5, limit=10, server='https://evil.test'))
        assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
        page = result.json()
        assert (page['offset'], page['limit']) == (5, 10)
        station = page['stations'][0]
        assert station['stationuuid'] == UUID_TEXT
        assert station['tags'] == ['jazz', 'rock']
        assert station['playable_url'] == ROW['url']
        assert station['lastcheckok'] is True and station['hls'] is False
        assert dict(seen[-1].url.params) == dict(name='Radio',countrycode='AT',tag='jazz',offset='5',limit='10',hidebroken='true',order='name')
        assert api.get('/api/v1/radio-browser/stations/'+UUID_TEXT).json()['stationuuid'] == UUID_TEXT
        assert seen[-1].url.path == '/json/stations/byuuid/'+UUID_TEXT
        assert api.get('/api/v1/radio-browser/countrycodes').json() == [dict(name='AT', stationcount=14)]
        assert api.get('/api/v1/radio-browser/tags', params={'name':'ja/zz', 'limit':10, 'offset':2}).json() == [dict(name='jazz',stationcount=12)]
        assert '%2F' in seen[-1].url.raw_path.decode()
        assert api.get('/api/v1/sources').json() == before
        for params in ({'limit':101}, {'limit':0}, {'offset':-1}, {'countrycode':'Austria'}):
            assert api.get('/api/v1/radio-browser/stations/search',params=params).status_code == 422
        assert api.get('/api/v1/radio-browser/stations/not-a-uuid').status_code == 422


def test_empty_results_and_broken_filter(tmp_path):
    rows = []
    with TestClient(create_app(Settings(tmp_path/'rb.db'), radio_browser_client=make_client(lambda r:httpx.Response(200,json=rows)))) as api:
        assert api.get('/api/v1/radio-browser/stations/search').json()['stations'] == []
        assert api.get('/api/v1/radio-browser/stations/'+UUID_TEXT).status_code == 404
        rows.append(ROW | {'lastcheckok':0, 'hls':1})
        assert api.get('/api/v1/radio-browser/stations/search').json()['stations'] == []
        assert len(api.get('/api/v1/radio-browser/stations/search?hidebroken=false').json()['stations']) == 1
        assert api.get('/api/v1/radio-browser/stations/'+UUID_TEXT).json()['lastcheckok'] is False


@pytest.mark.parametrize('failure', ['network','tls','5xx','json','shape','identity','size','compression','redirect'])
def test_failures_bounded_and_sanitized(failure):
    attempts = []
    def handler(request):
        attempts.append(request)
        if failure in ('network','tls'):
            raise httpx.ConnectError('secret password at upstream.test', request=request)
        if failure == '5xx': return httpx.Response(503, text='secret')
        if failure == 'json': return httpx.Response(200, text='not json secret')
        if failure == 'shape': return httpx.Response(200, json={})
        if failure == 'identity': return httpx.Response(200, json=[ROW | {'stationuuid':'not a UUID'}])
        if failure == 'size': return httpx.Response(200, content=b' '*(MAX_BYTES+1))
        if failure == 'compression': return httpx.Response(200, content=b'', headers={'content-encoding':'gzip'})
        return httpx.Response(302, headers={'location':'http://evil.test/private'})
    async def run():
        client=make_client(handler)
        try:
            with pytest.raises(DomainError) as error:
                await client.search()
            assert error.value.status == 503 and 'secret' not in str(error.value)
            assert len(attempts) == 2
        finally: await client.close()
    asyncio.run(run())


def test_mirror_retry_and_wrong_uuid():
    attempts=[]
    def handler(request):
        attempts.append(request)
        return httpx.Response(503) if len(attempts)==1 else httpx.Response(200,json=[ROW])
    async def run():
        client=make_client(handler)
        try:
            assert (await client.search()).stations[0].name == ROW['name']
            assert attempts[0].url.host != attempts[1].url.host
            with pytest.raises(DomainError):
                await client.lookup('11111111-1111-1111-1111-111111111111')
        finally: await client.close()
    asyncio.run(run())


def test_url_policy():
    assert playable_url(ROW['url'],ROW['url_resolved']) == ROW['url']
    assert playable_url('https://radio.test/list.pls','https://cdn.test/audio') == 'https://cdn.test/audio'
    assert playable_url('https://radio.test/list.m3u8','https://cdn.test/audio',True) == 'https://radio.test/list.m3u8'
    assert playable_url('ftp://radio.test/audio','https://cdn.test/audio') == 'https://cdn.test/audio'
    assert playable_url('file:///private',None) is None
    assert playable_url('https://radio.test/list.m3u',None) is None


def test_dns_discovery_and_fallback(monkeypatch):
    async def run():
        loop=asyncio.get_running_loop()
        async def addresses(*args, **kwargs):
            return [(2,1,6,'',('192.0.2.1',443)),(2,1,6,'',('192.0.2.2',443))]
        async def names(address,*args):
            return ('fresh.api.radio-browser.info' if address[0].endswith('.1') else 'evil.test','443')
        monkeypatch.setattr(loop,'getaddrinfo',addresses)
        monkeypatch.setattr(loop,'getnameinfo',names)
        assert await discover_servers() == ['fresh.api.radio-browser.info']
        async def fail(*args,**kwargs): raise OSError('DNS unavailable')
        monkeypatch.setattr(loop,'getaddrinfo',fail)
        assert await discover_servers() == ['de1.api.radio-browser.info','at1.api.radio-browser.info']
    asyncio.run(run())
