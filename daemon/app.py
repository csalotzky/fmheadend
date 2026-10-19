import argparse
import asyncio
from contextlib import asynccontextmanager
import logging
from types import SimpleNamespace
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from . import __version__
from .diagnostics import RecentLogs
from .api.routes import router
from .api.radio_browser import router as radio_browser_router
from .radio_browser import RadioBrowserClient
from .sources.radio_recovery import RadioRecoveryTask
from .config import Settings, audio_telemetry_hz
from .audio_telemetry import AudioTelemetry
from .audio_profile_files import AudioProfileFiles
from .control import ControlExecutor, DiscoveryExecutor
from .database import Database, SCHEMA_VERSION
from .regions import PLANS
from .webui import mount_webui
from .errors import DependencyUnavailable, DomainError
from .filesystem import FilesystemBrowser
from .alsa import AlsaDiscovery
from .modulators.manager import ModulatorManager
from .mux.client import MockMuxClient
from .mux.unix_client import UnixMuxClient
from .recovery import Recovery
from .rds.radiotext import RadioTextService
from .rds.refill import RdsRefill, POLL_SECONDS
from .rp2354.client import MockRp2354Client
from .rp2354.linux_client import Rp2354Client
from .services import ConfigurationService
from .sources.manager import SourceManager
from .sources.pipeline import MockPipeline
from .sources.gst_pipeline import GstPipeline
from .sources.output import (DEFAULT_STAGING_BUFFER_MS, DEFAULT_CONSUMER_BUFFER_MS,
                             DEFAULT_STAGING_MAX_AGE_MS, pcm_capacity_bytes)
from .status.service import StatusService

logger = logging.getLogger(__name__)


def create_app(settings=None, *, rp=None, mux=None, pipeline_factory=MockPipeline, clock=time.monotonic, mux_factory=None, radio_browser_client=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def service_lifespan(app):
        control = ControlExecutor()
        app.state.control = control
        def initialize():
            database = Database(settings.database_path, initial_preemphasis_us=75 if settings.region == 2 else 50)
            profile_files = AudioProfileFiles(settings.audio_profiles_default_dir, settings.audio_profiles_custom_dir,
                is_referenced=lambda path: any(s.audio_profile_id == path for s in database.list('sources')),
                is_system_default=lambda path: database.get_system().default_audio_profile_id == path)
            sources = SourceManager(database, pipeline_factory, profile_files, clock=clock,
                pcm_staging_buffer_ms=settings.pcm_staging_buffer_ms,
                pcm_staging_max_age_ms=settings.pcm_staging_max_age_ms,
                pcm_consumer_buffer_ms=settings.pcm_consumer_buffer_ms)
            rp_client = rp or MockRp2354Client()
            mux_client = mux or (mux_factory(sources) if mux_factory else MockMuxClient())
            modulators = ModulatorManager(database, sources, rp_client, mux_client, settings)
            rt = RadioTextService(database, sources, clock)
            rt.validate_target = modulators.require_available
            rds = RdsRefill(database, modulators, rt, rp_client, clock)
            rds.channel_available = lambda channel: channel in modulators.physical_ids
            modulators.rds = rds
            recovery = Recovery(modulators, rds, clock)
            modulators.recovery = recovery
            rds.on_failure = recovery.communication_failed
            rds.can_communicate = lambda: (recovery.can_communicate() and recovery.availability_ready
                                          and not recovery.capability_transition)
            rds.channel_ready = lambda channel: channel not in recovery.pending
            rds.suspended = True
            config = ConfigurationService(database, sources, modulators)
            status = StatusService(modulators, sources, rp_client, mux_client, clock, recovery,
                                   config=config, profile_files=profile_files, radiotext=rt)
            filesystem = FilesystemBrowser(settings.media_browser_roots)
            status.info.update(database_schema_version=SCHEMA_VERSION,
                               media_backend="MOCK" if getattr(pipeline_factory, "mocked", True) else "GStreamer")
            status.publish()
            return SimpleNamespace(
                profile_files=profile_files, filesystem=filesystem, alsa=AlsaDiscovery(),
                database=database, rp=rp_client, mux=mux_client, config=config, sources=sources, modulators=modulators, rt=rt, rds=rds, status=status, recovery=recovery,
                info=status.info,
            )
        try:
            app.state.services = await control.call(initialize)
        except BaseException:
            control.close()
            raise
        services = app.state.services
        discovery = DiscoveryExecutor()
        app.state.discovery = discovery
        services.radio_browser = radio_browser_client or RadioBrowserClient()
        radio_recovery = RadioRecoveryTask(control, services.sources.radio_recovery, services.radio_browser)
        services.audio_telemetry = (AudioTelemetry(settings.audio_telemetry_socket,
            settings.audio_telemetry_hz, control, services.sources) if settings.audio_telemetry_socket else None)
        next_snapshot = 0.0

        def publish_status(*, force=True):
            nonlocal next_snapshot
            now = time.monotonic()
            if not force and now < next_snapshot:
                return
            next_snapshot = now + 1.0
            # Observation errors must not retroactively fail a confirmed action.
            try:
                interval = services.config.get_system().controller_status_poll_interval_ms / 1000
                services.status.publish(max_age_s=max(2.0, 2 * interval), configuration_changed=force)
            except Exception:
                services.status.publication_failed()
                logger.exception('Status publication failed; retaining stale observation')
        app.state.publish_status = publish_status

        next_poll = 0.0
        def tick():
            nonlocal next_poll
            services.rt.expire()
            services.recovery.service()
            services.rds.tick()
            now = time.monotonic()
            if now >= next_poll:
                services.status.refresh(publish=False)
                next_poll = now + services.config.get_system().controller_status_poll_interval_ms / 1000
            publish_status(force=False)
            interval = min(POLL_SECONDS, max(0.01, next_poll - time.monotonic()))
            delay = services.recovery.retry_delay
            return min(interval, delay) if delay > 0 else interval

        def maintenance_tick():
            try:
                return tick()
            except Exception as exc:
                services.status.publication_failed()
                if not isinstance(exc, DependencyUnavailable):
                    # Unknown partial progress is not permission to replay control.
                    services.recovery.stopping = True
                    services.rds.suspended = True
                raise

        async def maintenance():
            while True:
                try:
                    interval = await control.call(maintenance_tick)
                except asyncio.CancelledError:
                    raise
                except DependencyUnavailable:
                    logger.exception('Maintenance dependency failed; retrying in 250 ms')
                    interval = 0.25
                except DomainError as exc:
                    if exc.error != "CONTROL_BUSY":
                        logger.critical('Maintenance stopped; daemon restart required', exc_info=True)
                        raise
                    interval = 0.25
                except Exception:
                    logger.critical('Maintenance stopped; daemon restart required', exc_info=True)
                    raise
                await asyncio.sleep(min(0.25, interval))

        task = asyncio.create_task(maintenance())
        radio_recovery.start()
        logger.info("fmheadendd started; media backend: %s; Controller control: %s",
                    pipeline_factory.__name__,
                    {'Rp2354Client': 'ControllerClient', 'MockRp2354Client': 'MockControllerClient'}.get(
                        type(services.rp).__name__, type(services.rp).__name__))
        logger.info("PCM buffers: staging_capacity_ms=%s staging_stale_age_ms=%s consumer_buffer_ms=%s",
                    settings.pcm_staging_buffer_ms, settings.pcm_staging_max_age_ms, settings.pcm_consumer_buffer_ms)
        try:
            if services.audio_telemetry is not None:
                await services.audio_telemetry.start()
            yield
        finally:
            cleanup_errors = []
            discovery.closing = True

            def cleanup(name, operation):
                try:
                    operation()
                except Exception as exc:
                    logger.exception('%s shutdown failed', name)
                    cleanup_errors.append(exc)

            if services.audio_telemetry is not None:
                try:
                    await services.audio_telemetry.close()
                except Exception as exc:
                    logger.exception('Audio telemetry shutdown failed')
                    cleanup_errors.append(exc)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # Expected cancellation of our maintenance task.
            except Exception as exc:
                logger.exception('Maintenance task failed before shutdown')
                cleanup_errors.append(exc)
            for name, operation in (
                ('Radio Browser recovery', radio_recovery.close),
                ('Radio Browser client', services.radio_browser.close),
            ):
                try:
                    await operation()
                except Exception as exc:
                    logger.exception('%s shutdown failed', name)
                    cleanup_errors.append(exc)

            def shutdown():
                cleanup('RF/recovery', services.recovery.shutdown)
                cleanup('Sources', services.sources.close)
                cleanup('Mux', services.mux.close)
                if isinstance(services.rp, Rp2354Client):
                    cleanup('Controller', services.rp.close)
                cleanup('Database', services.database.close)
            try:
                # Queue behind any shielded in-flight work, but never reject
                # essential cleanup because the normal request queue is full.
                await asyncio.shield(asyncio.get_running_loop().run_in_executor(control.executor, shutdown))
            finally:
                cleanup('Control executor', control.close)
            try:
                # Independent probes own no DB/live pipeline state. Complete RF
                # and source cleanup first, then drain their owned resources.
                await discovery.close()
            except Exception as exc:
                logger.exception('Discovery shutdown failed')
                cleanup_errors.append(exc)
            if cleanup_errors:
                raise ExceptionGroup('fmheadendd shutdown failures', cleanup_errors)
            logger.info("fmheadendd shutdown completed")

    @asynccontextmanager
    async def lifespan(app):
        with RecentLogs() as logs:
            app.state.recent_logs = logs
            async with service_lifespan(app):
                yield

    app = FastAPI(title="fmheadend", version=__version__, lifespan=lifespan,
                  description="V1 headend configuration, runtime status and diagnostics.")

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": exc.error, "message": exc.message})

    async def validation_error(request, exc):
        messages = [f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()]
        return JSONResponse(status_code=422, content={"error": "VALIDATION_ERROR", "message": "; ".join(messages)})

    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(ValidationError, validation_error)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code, headers=exc.headers,
                            content={"error": f"HTTP_{exc.status_code}", "message": str(exc.detail)})

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc):
        logger.error("Unhandled API error", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": "INTERNAL_ERROR", "message": "Internal server error"})

    app.include_router(router)
    app.include_router(radio_browser_router)
    if settings.webui_dir is not None:
        mount_webui(app, settings.webui_dir)
    return app


def pcm_buffer_ms(text):
    try:
        if not text.isascii() or not text.isdecimal():
            raise ValueError("PCM buffer milliseconds must be a positive integer")
        value = int(text)
        pcm_capacity_bytes(value)
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def argument_parser():
    parser = argparse.ArgumentParser(description="fmheadendd media and Controller control")
    parser.add_argument("--database", default="/var/lib/fmheadend/fmheadend.db")
    parser.add_argument("--host", help="Override persistent web_listen_address")
    parser.add_argument("--port", type=int, help="Override persistent web_port")
    parser.add_argument("--mock", action="store_true", help="Use mock sources, mux and Controller control")
    parser.add_argument("--mock-rp", action="store_true", help="Use the original high-level mock Controller client")
    parser.add_argument("--i2c-device", help="Override persistent i2c_device, e.g. /dev/i2c-1")
    parser.add_argument("--rp-i2c-address", type=lambda value: int(value, 0), help="7-bit slave address, decimal or 0x hex; no default")
    parser.add_argument("--i2c-timeout-ms", type=int, default=200)
    parser.add_argument("--mock-mux", action="store_true", help="Use real sources with the M2 mock mux")
    parser.add_argument("--mux-runtime-dir", default="/run/fmheadend")
    parser.add_argument("--pcm-staging-buffer-ms", type=pcm_buffer_ms, default=DEFAULT_STAGING_BUFFER_MS,
                        help="Staging byte capacity / burst headroom in ms (default 200); independent of staging maximum age")
    parser.add_argument("--pcm-staging-max-age-ms", type=pcm_buffer_ms, default=DEFAULT_STAGING_MAX_AGE_MS,
                        help="Staging maximum PCM age in ms (default 100); independent of staging byte capacity")
    parser.add_argument("--pcm-consumer-buffer-ms", type=pcm_buffer_ms, default=DEFAULT_CONSUMER_BUFFER_MS,
                        help="Per-consumer PCM capacity and maximum age in ms (default 300)")
    parser.add_argument("--region", type=int, choices=sorted(PLANS), default=1, help="Frequency selection plan: 0 Expert, 1 Europe (default), 2 US, 3 Japan")
    parser.add_argument("--webui-dir", help="Production Angular browser assets directory (optional)")
    parser.add_argument("--media-browser-root", action="append", default=[],
                        help="Additional absolute root available to the LOCAL source path browser; repeatable")
    parser.add_argument("--audio-profiles-default-dir", default="/usr/share/fmheadend/audio-profiles/default")
    parser.add_argument("--audio-profiles-custom-dir", default="/var/lib/fmheadend/audio-profiles/custom")
    parser.add_argument("--audio-telemetry-socket", help="Optional local DSP diagnostics Unix socket path")
    parser.add_argument("--audio-telemetry-hz", type=audio_telemetry_hz, default=50.0,
                        help="Diagnostic sampling rate, 10..100 Hz (default 50)")
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    settings = Settings(args.database, args.region, args.webui_dir,
                        args.audio_profiles_default_dir, args.audio_profiles_custom_dir,
                        pcm_staging_buffer_ms=args.pcm_staging_buffer_ms,
                        pcm_staging_max_age_ms=args.pcm_staging_max_age_ms,
                        pcm_consumer_buffer_ms=args.pcm_consumer_buffer_ms,
                        media_browser_roots=tuple(args.media_browser_root),
                        audio_telemetry_socket=args.audio_telemetry_socket,
                        audio_telemetry_hz=args.audio_telemetry_hz)
    database = Database(args.database, initial_preemphasis_us=75 if settings.region == 2 else 50)
    try:
        config = database.get_system()
    finally:
        database.close()
    logging.basicConfig(level=config.log_level, format="%(levelname)s %(name)s %(message)s")
    import uvicorn
    mux_factory = None if args.mock or args.mock_mux else lambda sources: UnixMuxClient(sources.output_for, args.mux_runtime_dir)
    try:
        rp = MockRp2354Client() if args.mock or args.mock_rp else Rp2354Client(
            args.i2c_device or config.i2c_device,
            config.controller_i2c_address if args.rp_i2c_address is None else args.rp_i2c_address,
            timeout_ms=args.i2c_timeout_ms)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(create_app(settings, rp=rp, pipeline_factory=MockPipeline if args.mock else GstPipeline, mux_factory=mux_factory), host=args.host or config.web_listen_address,
                port=args.port if args.port is not None else config.web_port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
