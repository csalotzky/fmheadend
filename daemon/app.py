import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import logging
from types import SimpleNamespace
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from . import __version__
from .api.routes import router
from .config import Settings
from .control import ControlExecutor
from .database import Database, SCHEMA_VERSION
from .regions import PLANS
from .webui import mount_webui
from .errors import DomainError
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
from .status.service import StatusService

logger = logging.getLogger(__name__)


def create_app(settings=None, *, rp=None, mux=None, pipeline_factory=MockPipeline, clock=time.monotonic, mux_factory=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        control = ControlExecutor()
        app.state.control = control
        def initialize():
            database = Database(settings.database_path)
            sources = SourceManager(database, pipeline_factory)
            rp_client = rp or MockRp2354Client()
            mux_client = mux or (mux_factory(sources) if mux_factory else MockMuxClient())
            modulators = ModulatorManager(database, sources, rp_client, mux_client, settings)
            rt = RadioTextService(database, sources, clock)
            rds = RdsRefill(database, modulators, rt, rp_client, clock)
            modulators.rds = rds
            recovery = Recovery(modulators, rds, clock)
            modulators.recovery = recovery
            rds.on_failure = recovery.communication_failed
            rds.can_communicate = recovery.can_communicate
            rds.channel_ready = lambda channel: channel not in recovery.pending
            rds.suspended = True
            status = StatusService(modulators, sources, rp_client, mux_client, clock, recovery)
            status.info.update(frequency_plan=PLANS[settings.region].as_dict(),
                               database_schema_version=SCHEMA_VERSION,
                               media_backend="MOCK" if getattr(pipeline_factory, "mocked", True) else "GStreamer")
            config = ConfigurationService(database, sources, modulators)
            return SimpleNamespace(
                database=database, rp=rp_client, mux=mux_client, config=config, sources=sources, modulators=modulators, rt=rt, rds=rds, status=status, recovery=recovery,
                info=status.info,
            )
        try:
            app.state.services = await control.call(initialize)
        except BaseException:
            control.close()
            raise
        services = app.state.services

        next_poll = 0.0
        def tick():
            nonlocal next_poll
            services.rt.expire()
            services.recovery.service()
            services.rds.tick()
            now = time.monotonic()
            if now >= next_poll:
                services.status.refresh()
                next_poll = now + services.config.get_system().rp_status_poll_interval_ms / 1000
            interval = min(POLL_SECONDS, max(0.01, next_poll - time.monotonic()))
            delay = services.recovery.retry_delay
            return min(interval, delay) if delay > 0 else interval

        async def maintenance():
            while True:
                try:
                    interval = await control.call(tick)
                except DomainError as exc:
                    if exc.error != "CONTROL_BUSY":
                        raise
                    interval = 0.25
                await asyncio.sleep(min(0.25, interval))

        task = asyncio.create_task(maintenance())
        logger.info("fmheadendd started; media backend: %s; RP2354 control: %s",
                    pipeline_factory.__name__, type(services.rp).__name__)
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            def shutdown():
                try:
                    try:
                        services.recovery.shutdown()
                    finally:
                        services.sources.close()
                finally:
                    try:
                        services.mux.close()
                    finally:
                        try:
                            if isinstance(services.rp, Rp2354Client):
                                services.rp.close()
                        finally:
                            services.database.close()
            try:
                await control.call(shutdown)
            finally:
                control.close()
                logger.info("fmheadendd shutdown completed")

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
    if settings.webui_dir is not None:
        mount_webui(app, settings.webui_dir)
    return app


def argument_parser():
    parser = argparse.ArgumentParser(description="fmheadendd media and RP2354 control")
    parser.add_argument("--database", default="/var/lib/fmheadend/fmheadend.db")
    parser.add_argument("--host", help="Override persistent web_listen_address")
    parser.add_argument("--port", type=int, help="Override persistent web_port")
    parser.add_argument("--mock", action="store_true", help="Use mock sources, mux and RP2354 control")
    parser.add_argument("--mock-rp", action="store_true", help="Use the original high-level mock RP2354 client")
    parser.add_argument("--i2c-device", help="Override persistent i2c_device, e.g. /dev/i2c-1")
    parser.add_argument("--rp-i2c-address", type=lambda value: int(value, 0), help="7-bit slave address, decimal or 0x hex; no default")
    parser.add_argument("--i2c-timeout-ms", type=int, default=200)
    parser.add_argument("--mock-mux", action="store_true", help="Use real sources with the M2 mock mux")
    parser.add_argument("--mux-runtime-dir", default="/run/fmheadend")
    parser.add_argument("--region", type=int, choices=sorted(PLANS), default=1, help="Frequency selection plan: 0 Expert, 1 Europe (default), 2 US, 3 Japan")
    parser.add_argument("--webui-dir", help="Production Angular browser assets directory (optional)")
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    settings = Settings(args.database, args.region, args.webui_dir)
    database = Database(args.database)
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
            config.rp2354_i2c_address if args.rp_i2c_address is None else args.rp_i2c_address,
            timeout_ms=args.i2c_timeout_ms)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(create_app(settings, rp=rp, pipeline_factory=MockPipeline if args.mock else GstPipeline, mux_factory=mux_factory), host=args.host or config.web_listen_address,
                port=args.port if args.port is not None else config.web_port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
