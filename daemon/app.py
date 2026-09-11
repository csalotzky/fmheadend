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
from .database import Database
from .errors import DomainError
from .modulators.manager import ModulatorManager
from .mux.client import MockMuxClient
from .mux.unix_client import UnixMuxClient
from .rds.radiotext import RadioTextService
from .rp2354.client import MockRp2354Client
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
            status = StatusService(modulators, sources, rp_client, mux_client, clock)
            config = ConfigurationService(database, sources, modulators)
            return SimpleNamespace(
                database=database, mux=mux_client, config=config, sources=sources, modulators=modulators, rt=rt, status=status,
                info={"fmheadend_version": __version__, "rp_protocol_version": 1,
                      "rp_firmware_version": "mock", "hardware_revision": "mock", "modulator_count": 8, "mocked": True},
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
            now = time.monotonic()
            if now >= next_poll:
                services.status.refresh()
                next_poll = now + services.config.get_system().rp_status_poll_interval_ms / 1000
            return max(0.01, next_poll - time.monotonic())

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
        logger.info("fmheadendd started; media backend: %s; RP2354 control: mock", pipeline_factory.__name__)
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            def shutdown():
                try:
                    services.modulators.shutdown()
                    services.sources.close()
                finally:
                    try:
                        services.mux.close()
                    finally:
                        services.database.close()
            try:
                await control.call(shutdown)
            finally:
                control.close()

    app = FastAPI(title="fmheadend", version=__version__, lifespan=lifespan,
                  description="Milestone 3 media and Unix mux integration. RP2354 control remains mocked.")

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
    return app


def main():
    parser = argparse.ArgumentParser(description="fmheadendd source ingest with mock hardware")
    parser.add_argument("--database", default="/var/lib/fmheadend/fmheadend.db")
    parser.add_argument("--host", help="Override persistent web_listen_address")
    parser.add_argument("--port", type=int, help="Override persistent web_port")
    parser.add_argument("--mock", action="store_true", help="Use mock sources and mux; RP2354 control is always mocked")
    parser.add_argument("--mock-mux", action="store_true", help="Use real sources with the M2 mock mux")
    parser.add_argument("--mux-runtime-dir", default="/run/fmheadend")
    parser.add_argument("--fm-band-min-khz", type=int, default=87500)
    parser.add_argument("--fm-band-max-khz", type=int, default=108000)
    args = parser.parse_args()
    settings = Settings(args.database, args.fm_band_min_khz, args.fm_band_max_khz)
    database = Database(args.database)
    try:
        config = database.get_system()
    finally:
        database.close()
    logging.basicConfig(level=config.log_level, format="%(levelname)s %(name)s %(message)s")
    import uvicorn
    mux_factory = None if args.mock or args.mock_mux else lambda sources: UnixMuxClient(sources.output_for, args.mux_runtime_dir)
    uvicorn.run(create_app(settings, pipeline_factory=MockPipeline if args.mock else GstPipeline, mux_factory=mux_factory), host=args.host or config.web_listen_address,
                port=args.port if args.port is not None else config.web_port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
