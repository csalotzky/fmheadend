from fastapi import APIRouter, Request, Response
from ..models.configuration import (
    AudioProfile, AudioProfileConfig, AudioProfilePatch, ErrorResponse,
    InputSource, InputSourceConfig, InputSourcePatch, Modulator, ModulatorPatch,
    RtOverride, RtStatus, SourceTestResult, SystemConfig, SystemConfigPatch,
)
from ..models.runtime_status import HeadendStatus, InputSourceStatus, ModulatorStatus

router = APIRouter(prefix="/api/v1", responses={
    code: {"model": ErrorResponse} for code in (400, 404, 409, 422, 500, 503)
})


@router.get("/sources", response_model=list[InputSource], tags=["sources"])
async def list_sources(request: Request):
    return request.app.state.services.config.list("sources")


@router.post("/sources", response_model=InputSource, status_code=201, tags=["sources"])
async def create_source(body: InputSourceConfig, request: Request):
    return request.app.state.services.config.create("sources", body)


@router.post("/sources/test", response_model=SourceTestResult, tags=["sources"])
async def test_unsaved_source(body: InputSourceConfig, request: Request):
    """Mock-only probe; does not persist or assign the submitted configuration."""
    return request.app.state.services.sources.test(body)


@router.get("/sources/{id}", response_model=InputSource, tags=["sources"])
async def get_source(id: int, request: Request):
    return request.app.state.services.config.get("sources", id)


@router.patch("/sources/{id}", response_model=InputSource, tags=["sources"])
async def update_source(id: int, body: InputSourcePatch, request: Request):
    """Replace supplied fields (including the entire config object). Active sources return 409."""
    return request.app.state.services.config.update("sources", id, body)


@router.delete("/sources/{id}", status_code=204, tags=["sources"])
async def delete_source(id: int, request: Request):
    request.app.state.services.config.delete("sources", id)
    return Response(status_code=204)


@router.post("/sources/{id}/test", response_model=SourceTestResult, tags=["sources"])
async def test_saved_source(id: int, request: Request):
    services = request.app.state.services
    return services.sources.test(services.config.get("sources", id))


@router.get("/sources/{id}/status", response_model=InputSourceStatus, tags=["sources"])
async def source_status(id: int, request: Request):
    return request.app.state.services.sources.status(id)


@router.get("/audio-profiles", response_model=list[AudioProfile], tags=["audio profiles"])
async def list_profiles(request: Request):
    return request.app.state.services.config.list("audio_profiles")


@router.post("/audio-profiles", response_model=AudioProfile, status_code=201, tags=["audio profiles"])
async def create_profile(body: AudioProfileConfig, request: Request):
    return request.app.state.services.config.create("audio_profiles", body)


@router.get("/audio-profiles/{id}", response_model=AudioProfile, tags=["audio profiles"])
async def get_profile(id: int, request: Request):
    return request.app.state.services.config.get("audio_profiles", id)


@router.patch("/audio-profiles/{id}", response_model=AudioProfile, tags=["audio profiles"])
async def update_profile(id: int, body: AudioProfilePatch, request: Request):
    return request.app.state.services.config.update("audio_profiles", id, body)


@router.delete("/audio-profiles/{id}", status_code=204, tags=["audio profiles"])
async def delete_profile(id: int, request: Request):
    request.app.state.services.config.delete("audio_profiles", id)
    return Response(status_code=204)


@router.get("/modulators", response_model=list[Modulator], tags=["modulators"])
async def list_modulators(request: Request):
    return request.app.state.services.modulators.list()


@router.post("/modulators/start-all", tags=["modulators"])
async def start_all(request: Request):
    """Attempt every modulator; return per-ID status or error, including missing assignments."""
    return request.app.state.services.modulators.all(True)


@router.post("/modulators/stop-all", tags=["modulators"])
async def stop_all(request: Request):
    """Attempt every modulator even if one dependency operation fails."""
    return request.app.state.services.modulators.all(False)


@router.get("/modulators/{id}", response_model=Modulator, tags=["modulators"])
async def get_modulator(id: int, request: Request):
    return request.app.state.services.modulators.get(id)


@router.patch("/modulators/{id}", response_model=Modulator, tags=["modulators"])
async def update_modulator(id: int, body: ModulatorPatch, request: Request):
    """Commit desired config, then reapply an active TX. Apply failures preserve config."""
    return request.app.state.services.modulators.update(id, body)


@router.post("/modulators/{id}/start", response_model=ModulatorStatus, tags=["modulators"])
async def start_modulator(id: int, request: Request):
    """Persist enabled=true and start the mock TX."""
    return request.app.state.services.modulators.start(id)


@router.post("/modulators/{id}/stop", response_model=ModulatorStatus, tags=["modulators"])
async def stop_modulator(id: int, request: Request):
    """Persist enabled=false and stop RF before releasing the source."""
    return request.app.state.services.modulators.stop(id)


@router.put("/modulators/{id}/rds/rt", response_model=RtStatus, tags=["runtime RDS"])
async def set_rt(id: int, body: RtOverride, request: Request):
    """Set runtime-only text; optional TTL uses a monotonic clock."""
    return request.app.state.services.rt.set(id, body)


@router.get("/modulators/{id}/rds/rt", response_model=RtStatus, tags=["runtime RDS"])
async def get_rt(id: int, request: Request):
    """Inspect effective runtime text without adding it to dashboard status."""
    return request.app.state.services.rt.effective(id)


@router.delete("/modulators/{id}/rds/rt", status_code=204, tags=["runtime RDS"])
async def clear_rt(id: int, request: Request):
    request.app.state.services.rt.clear(id)
    return Response(status_code=204)


@router.get("/status", response_model=HeadendStatus, tags=["system"])
async def status(request: Request):
    return request.app.state.services.status.snapshot()


@router.get("/system/config", response_model=SystemConfig, tags=["system"])
async def get_system_config(request: Request):
    return request.app.state.services.config.get_system()


@router.patch("/system/config", response_model=SystemConfig, tags=["system"])
async def update_system_config(body: SystemConfigPatch, request: Request):
    """Persist settings. Listener/log changes take effect on next daemon startup."""
    return request.app.state.services.config.update_system(body)


@router.get("/system/info", tags=["system"])
async def system_info(request: Request):
    return request.app.state.services.info
