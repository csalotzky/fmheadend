from fastapi import APIRouter, Request, Response
from ..control import control_route
from ..models.configuration import (
    ErrorResponse,
    InputSource, InputSourceConfig, InputSourcePatch, SourceSaveResult, Modulator, ModulatorPatch,
    RtOverride, RtStatus, SourceTestResult, SystemConfig, SystemConfigPatch,
)
from ..models.runtime_status import HeadendStatus, InputSourceStatus, ModulatorStatus, ModulatorSaveResult
from ..models.audio_profile_file import AudioProfileFile, CloneProfileFile, ProfileFileResult, ProfileFileSummary, ProfileSaveResult

router = APIRouter(prefix="/api/v1", responses={
    code: {"model": ErrorResponse} for code in (400, 404, 409, 422, 500, 503)
})


@router.get('/system/logs', response_model=list[dict], tags=['system'])
async def recent_logs(request: Request):
    """Newest 100 backend log entries since startup; no hardware or journal I/O."""
    return request.app.state.recent_logs.snapshot()


@router.get("/sources", response_model=list[InputSource], tags=["sources"])
@control_route
def list_sources(request: Request):
    return request.app.state.services.config.list("sources")


@router.post("/sources", response_model=InputSource, status_code=201, tags=["sources"])
@control_route
def create_source(body: InputSourceConfig, request: Request):
    return request.app.state.services.config.create("sources", body)


@router.post("/sources/test", response_model=SourceTestResult, tags=["sources"])
@control_route
def test_unsaved_source(body: InputSourceConfig, request: Request):
    """Probe without persisting or assigning configuration. Uses the selected media backend."""
    return request.app.state.services.sources.test(body)


@router.get("/sources/{id}", response_model=InputSource, tags=["sources"])
@control_route
def get_source(id: int, request: Request):
    return request.app.state.services.config.get("sources", id)


@router.patch("/sources/{id}", response_model=SourceSaveResult, tags=["sources"])
@control_route
def update_source(id: int, body: InputSourcePatch, request: Request):
    """Persist supplied fields, then apply to this Source's existing audio pipeline."""
    return request.app.state.services.config.update("sources", id, body)


@router.delete("/sources/{id}", status_code=204, tags=["sources"])
@control_route
def delete_source(id: int, request: Request):
    request.app.state.services.config.delete("sources", id)
    return Response(status_code=204)


@router.post("/sources/{id}/test", response_model=SourceTestResult, tags=["sources"])
@control_route
def test_saved_source(id: int, request: Request):
    services = request.app.state.services
    return services.sources.test(services.config.get("sources", id))


@router.get("/sources/{id}/status", response_model=InputSourceStatus, tags=["sources"])
@control_route
def source_status(id: int, request: Request):
    return request.app.state.services.sources.status(id)


@router.get("/audio-profiles/files", response_model=list[ProfileFileSummary], tags=["audio profiles"])
@control_route
def list_profile_files(request: Request):
    return request.app.state.services.profile_files.list()


@router.post("/audio-profiles/files/custom", response_model=ProfileFileResult, status_code=201, tags=["audio profiles"])
@control_route
def create_profile_file(body: AudioProfileFile, request: Request):
    request.app.state.services.sources.validate_profile(body)
    return request.app.state.services.profile_files.create(body.model_dump())


@router.get('/audio-profiles/files/dsp', tags=['audio profiles'])
@control_route
def profile_dsp_editor(request: Request):
    from ..sources.dsp import editor_description
    return editor_description()


@router.get("/audio-profiles/files/{kind}/{filename}", response_model=ProfileFileResult, tags=["audio profiles"])
@control_route
def get_profile_file(kind: str, filename: str, request: Request):
    return request.app.state.services.profile_files.get(f"{kind}/{filename}")


@router.put("/audio-profiles/files/custom/{filename}", response_model=ProfileSaveResult, tags=["audio profiles"])
@control_route
def save_profile_file(filename: str, body: AudioProfileFile, request: Request):
    return request.app.state.services.sources.save_profile(f"custom/{filename}", body.model_dump())


@router.post("/audio-profiles/files/{kind}/{filename}/clone", response_model=ProfileFileResult, status_code=201, tags=["audio profiles"])
@control_route
def clone_profile_file(kind: str, filename: str, body: CloneProfileFile, request: Request):
    return request.app.state.services.profile_files.clone(f"{kind}/{filename}", body.name)


@router.delete("/audio-profiles/files/custom/{filename}", status_code=204, tags=["audio profiles"])
@control_route
def delete_profile_file(filename: str, request: Request):
    request.app.state.services.profile_files.delete(f"custom/{filename}")
    return Response(status_code=204)


@router.get("/modulators", response_model=list[Modulator], tags=["modulators"])
@control_route
def list_modulators(request: Request):
    return request.app.state.services.modulators.list()


@router.post("/modulators/start-all", tags=["modulators"])
@control_route
def start_all(request: Request):
    """Start configured, desired-enabled modulators only; return per-ID status or error."""
    return request.app.state.services.modulators.all(True)


@router.post("/modulators/stop-all", tags=["modulators"])
@control_route
def stop_all(request: Request):
    """Attempt every modulator even if one dependency operation fails."""
    # Operational stop preserves persistent desired-enabled flags.
    return request.app.state.services.modulators.all(False)


@router.get("/modulators/{id}", response_model=Modulator, tags=["modulators"])
@control_route
def get_modulator(id: int, request: Request):
    return request.app.state.services.modulators.get(id)


@router.patch("/modulators/{id}", response_model=Modulator, tags=["modulators"])
@control_route
def update_modulator(id: int, body: ModulatorPatch, request: Request):
    """Persist confirmed-OFF configuration without RF commands; active/uncertain TXs cannot be patched."""
    return request.app.state.services.modulators.update(id, body)


@router.post("/modulators/{id}/start", response_model=ModulatorStatus, tags=["modulators"])
@control_route
def start_modulator(id: int, request: Request):
    """Persist enabled=true and start the TX through the selected RP client."""
    return request.app.state.services.modulators.start(id)


@router.post('/modulators/{id}/save-and-resume', response_model=ModulatorSaveResult, tags=['modulators'])
@control_route
def save_and_resume_modulator(id: int, body: ModulatorPatch, request: Request):
    """Serialize optional STOP, persistence, optional START; report partial outcomes."""
    return request.app.state.services.modulators.save_and_resume(id, body)


@router.post("/modulators/{id}/stop", response_model=ModulatorStatus, tags=["modulators"])
@control_route
def stop_modulator(id: int, request: Request):
    """Persist enabled=false and stop RF before releasing the source."""
    return request.app.state.services.modulators.stop(id)


@router.put("/modulators/{id}/rds/rt", response_model=RtStatus, tags=["runtime RDS"])
@control_route
def set_rt(id: int, body: RtOverride, request: Request):
    """Set runtime-only text; optional TTL uses a monotonic clock."""
    return request.app.state.services.rt.set(id, body)


@router.get("/modulators/{id}/rds/rt", response_model=RtStatus, tags=["runtime RDS"])
@control_route
def get_rt(id: int, request: Request):
    """Inspect effective runtime text without adding it to dashboard status."""
    return request.app.state.services.rt.effective(id)


@router.delete("/modulators/{id}/rds/rt", status_code=204, tags=["runtime RDS"])
@control_route
def clear_rt(id: int, request: Request):
    request.app.state.services.rt.clear(id)
    return Response(status_code=204)


@router.get("/status", response_model=HeadendStatus, tags=["system"])
@control_route
def status(request: Request):
    return request.app.state.services.status.snapshot()


@router.get("/system/config", response_model=SystemConfig, tags=["system"])
@control_route
def get_system_config(request: Request):
    return request.app.state.services.config.get_system()


@router.patch("/system/config", response_model=SystemConfig, tags=["system"])
@control_route
def update_system_config(body: SystemConfigPatch, request: Request):
    """Persist settings. Listener/log changes take effect on next daemon startup."""
    return request.app.state.services.config.update_system(body)


@router.get("/system/info", tags=["system"])
@control_route
def system_info(request: Request):
    return request.app.state.services.info
