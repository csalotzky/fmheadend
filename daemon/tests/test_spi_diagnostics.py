import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.control import ControlExecutor
from fmheadendd.errors import DependencyUnavailable, DomainError
from fmheadendd.rp2354.client import MockRp2354Client
from fmheadendd.rp2354.protocol import SPI_DIAGNOSTIC_FIELDS
from fmheadendd.status.spi_diagnostics import SpiDiagnostics
from fmheadendd.tests.profile_support import profile_settings


def test_api_cache_age_failure_cooldown_and_recovery(tmp_path):
    now = [0.0]
    rp = MockRp2354Client()
    calls = []
    failed = [False]

    def query():
        assert threading.current_thread().name.startswith('headend-control')
        calls.append(now[0])
        if failed[0]:
            raise DependencyUnavailable('Controller I2C read failed')
        return dict.fromkeys(SPI_DIAGNOSTIC_FIELDS, len(calls))

    rp.get_spi_diagnostics = query
    app = create_app(profile_settings(tmp_path / 'headend.db'), rp=rp, clock=lambda: now[0])
    with TestClient(app) as api:
        assert calls == []  # Startup and ordinary status polling do not collect it.
        result = api.get('/api/v1/diagnostics/spi')
        assert result.status_code == 200
        assert result.headers['cache-control'] == 'no-store'
        assert result.json() == {**dict.fromkeys(SPI_DIAGNOSTIC_FIELDS, 1), 'observation_age_s': 0.0}
        now[0] = 4.9
        assert api.get('/api/v1/diagnostics/spi').json()['observation_age_s'] == 4.9
        assert len(calls) == 1
        now[0] = 5.0
        failed[0] = True
        result = api.get('/api/v1/diagnostics/spi')
        assert result.status_code == 503
        assert result.json() == {'error': 'DEPENDENCY_UNAVAILABLE', 'message': 'Controller I2C read failed'}
        now[0] = 5.9
        assert api.get('/api/v1/diagnostics/spi').status_code == 503
        assert len(calls) == 2
        now[0] = 6.0
        failed[0] = False
        assert api.get('/api/v1/diagnostics/spi').json()['lost'] == 3
        assert len(calls) == 3


@pytest.mark.parametrize('fail', [False, True])
def test_concurrent_requests_share_one_control_job_even_after_cancellation(fail):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        calls = []
        control = ControlExecutor()

        def query():
            calls.append(threading.current_thread().name)
            entered.set()
            assert release.wait(5)
            if fail:
                raise DependencyUnavailable('offline')
            return dict.fromkeys(SPI_DIAGNOSTIC_FIELDS, 7)

        cache = SpiDiagnostics(SimpleNamespace(get_spi_diagnostics=query))
        try:
            first = asyncio.create_task(cache.read(control))
            assert await asyncio.to_thread(entered.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            others = [asyncio.create_task(cache.read(control)) for _ in range(20)]
            await asyncio.sleep(0)
            assert control.pending == 1
            assert len(calls) == 1
            release.set()
            results = await asyncio.gather(*others, return_exceptions=True)
            if fail:
                assert all(isinstance(r, DomainError) and r.status == 503 for r in results)
                with pytest.raises(DomainError):
                    await cache.read(control)
            else:
                assert all(r['lost'] == 7 for r in results)
            assert len(calls) == 1
            assert calls[0].startswith('headend-control')
        finally:
            release.set()
            if cache._pending is not None:
                await asyncio.gather(cache._pending, return_exceptions=True)
            control.close()

    asyncio.run(scenario())
