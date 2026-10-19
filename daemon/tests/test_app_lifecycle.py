"""Lifecycle fault injection with real services and mock hardware only."""
import asyncio
import logging

import pytest

from fmheadendd.app import create_app
from fmheadendd.errors import DependencyUnavailable, DomainError
from fmheadendd.sources.radio_recovery import RadioRecoveryTask
from fmheadendd.tests.profile_support import profile_settings


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def track_cleanup(monkeypatch, app, events, failures=()):
    services = app.state.services
    for name, owner, method in (
        ('rf', services.recovery, 'shutdown'),
        ('sources', services.sources, 'close'),
        ('mux', services.mux, 'close'),
        ('database', services.database, 'close'),
        ('executor', app.state.control, 'close'),
    ):
        original = getattr(owner, method)

        def close(name=name, original=original):
            events.append(name)
            original()
            if name in failures:
                raise RuntimeError(f'{name} cleanup fault')

        monkeypatch.setattr(owner, method, close)


def test_dependency_failure_retries_maintenance(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    app = create_app(profile_settings(tmp_path / 'config.db'))

    async def run():
        async with app.router.lifespan_context(app):
            services = app.state.services
            original = services.rds.tick
            calls = []

            def tick():
                calls.append(True)
                if len(calls) == 1:
                    raise DependencyUnavailable('temporary dependency fault')
                original()

            monkeypatch.setattr(services.rds, 'tick', tick)
            await wait_for(lambda: len(calls) >= 2)
            assert not services.recovery.stopping
            assert not services.rds.suspended
            assert app.state.control.executor._shutdown is False

    asyncio.run(run())
    assert 'Maintenance dependency failed; retrying' in caplog.text
    assert 'temporary dependency fault' in caplog.text
    assert app.state.control.executor._shutdown


def test_unexpected_maintenance_failure_is_visible_and_cleanup_runs(tmp_path, monkeypatch, caplog):
    app = create_app(profile_settings(tmp_path / 'config.db'))
    events, calls = [], []

    async def run():
        async with app.router.lifespan_context(app):
            services = app.state.services
            track_cleanup(monkeypatch, app, events)

            def tick():
                calls.append(True)
                raise RuntimeError('unexpected maintenance fault')

            monkeypatch.setattr(services.rds, 'tick', tick)
            await wait_for(lambda: services.recovery.stopping)
            await asyncio.sleep(0.3)
            assert len(calls) == 1  # Unknown partial progress must not be replayed.
            assert services.rds.suspended
            assert services.status.published_snapshot().system.snapshot_stale
            with pytest.raises(DomainError):
                services.recovery.require_ready()

    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(run())
    assert [str(error) for error in caught.value.exceptions] == ['unexpected maintenance fault']
    assert events == ['rf', 'sources', 'mux', 'database', 'executor']
    assert app.state.services.rp.enabled == [False] * 8
    assert 'Maintenance stopped; daemon restart required' in caplog.text
    assert 'Maintenance task failed before shutdown' in caplog.text


def test_shutdown_reports_all_failures_and_preserves_cleanup_order(tmp_path, monkeypatch, caplog):
    app = create_app(profile_settings(tmp_path / 'config.db'))
    events = []
    original_close = RadioRecoveryTask.close

    async def failing_close(self):
        events.append('radio recovery')
        await original_close(self)
        raise RuntimeError('radio recovery cleanup fault')

    monkeypatch.setattr(RadioRecoveryTask, 'close', failing_close)

    async def run():
        async with app.router.lifespan_context(app):
            services = app.state.services
            track_cleanup(monkeypatch, app, events, failures=('sources', 'mux'))
            original = services.radio_browser.close

            async def browser_close():
                events.append('radio client')
                await original()

            monkeypatch.setattr(services.radio_browser, 'close', browser_close)

    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(run())
    assert [str(error) for error in caught.value.exceptions] == [
        'radio recovery cleanup fault', 'sources cleanup fault', 'mux cleanup fault']
    assert events == ['radio recovery', 'radio client', 'rf', 'sources', 'mux', 'database', 'executor']
    assert app.state.services.rp.enabled == [False] * 8
    assert app.state.control.executor._shutdown
    for message in ('Radio Browser recovery shutdown failed', 'Sources shutdown failed', 'Mux shutdown failed'):
        assert message in caplog.text


def test_lifespan_cancellation_still_cleans_up(tmp_path, monkeypatch):
    app = create_app(profile_settings(tmp_path / 'config.db'))
    events = []

    async def run():
        async with app.router.lifespan_context(app):
            track_cleanup(monkeypatch, app, events)
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert events == ['rf', 'sources', 'mux', 'database', 'executor']
    assert app.state.control.executor._shutdown
