from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from ..control import control_route
from ..errors import DomainError
from ..alsa import AlsaCaptureDevices
from ..models.configuration import (
    ErrorResponse,
    InputSource, InputSourceConfig, InputSourcePatch, SourceSaveResult, Modulator, ModulatorPatch,
    RtOverride, RtStatus, SourceTestResult, SystemConfig, SystemConfigPatch, SystemSaveResult,
)
from ..models.runtime_status import HeadendStatus, InputSourceStatus, ModulatorStatus, ModulatorSaveResult
from ..models.audio_profile_file import AudioProfileFile, CloneProfileFile, ProfileFileResult, ProfileFileSummary, ProfileSaveResult
from ..models.schedule import ScheduleConfig, SchedulePatch, ScheduleRule

router = APIRouter(prefix="/api/v1", responses={
    code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 422, 500, 503)
})

# A one-feed Podcast test can use 20 s for HTTP, 120 s for parser admission,
# and 45 s for execution. Leave room for bounded reap and result publication.
SINGLE_PODCAST_TEST_TIMEOUT_S = 195.0
ScheduleId = Annotated[int, Path(gt=0, le=2**63 - 1)]


def _source_test_timeout(source):
    if source.type == 'PODCAST' and len(source.config['feeds']) == 1:
        return SINGLE_PODCAST_TEST_TIMEOUT_S
    return None


def published_view(request, response, name):
    status = request.app.state.services.status
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Observation-Age-Seconds'] = f'{status.publication_age():.3f}'
    return status.published_view(name)


@router.get('/diagnostics/spi', response_model=dict, tags=['diagnostics'])
async def spi_diagnostics(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return await request.app.state.services.status.spi_diagnostics.read(request.app.state.control)


@router.get("/alsa/capture-devices", response_model=AlsaCaptureDevices, tags=["alsa"])
async def alsa_capture_devices(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return await request.app.state.discovery.call(request.app.state.services.alsa.capture_devices)


@router.get("/filesystem/locations", tags=["filesystem"])
async def filesystem_locations(request: Request):
    return await request.app.state.discovery.call(request.app.state.services.filesystem.locations)


@router.get("/filesystem/browse", tags=["filesystem"])
async def filesystem_browse(path: str, mode: str, request: Request):
    return await request.app.state.discovery.call(
        lambda: request.app.state.services.filesystem.browse(path, mode))


@router.get('/system/logs', response_model=list[dict], tags=['system'])
async def recent_logs(request: Request):
    """Newest 100 backend log entries since startup; no hardware or journal I/O."""
    return request.app.state.recent_logs.snapshot()


@router.get("/sources", response_model=list[InputSource], tags=["sources"])
async def list_sources(request: Request, response: Response):
    return published_view(request, response, 'sources')


@router.post("/sources", response_model=InputSource, status_code=201, tags=["sources"])
@control_route
def create_source(body: InputSourceConfig, request: Request):
    return request.app.state.services.config.create("sources", body)


@router.post("/sources/test", response_model=SourceTestResult, tags=["sources"])
async def test_unsaved_source(body: InputSourceConfig, request: Request):
    """Probe without persisting or assigning configuration. Uses the selected media backend."""
    probe = await request.app.state.control.call(lambda: request.app.state.services.sources.prepare_test(body))
    return await request.app.state.discovery.call(probe, timeout=_source_test_timeout(body))


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
async def test_saved_source(id: int, request: Request):
    services = request.app.state.services

    def prepare():
        source = services.config.get("sources", id)
        return services.sources.prepare_test(source), _source_test_timeout(source)

    probe, timeout = await request.app.state.control.call(prepare)
    return await request.app.state.discovery.call(probe, timeout=timeout)


@router.get("/sources/{id}/status", response_model=InputSourceStatus, tags=["sources"])
async def source_status(id: int, request: Request):
    return request.app.state.services.status.published_source(id)


@router.get("/audio-profiles/files", response_model=list[ProfileFileSummary], tags=["audio profiles"])
@control_route
def list_profile_files(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Observation-Age-Seconds'] = '0.000'
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
async def list_modulators(request: Request, response: Response):
    return published_view(request, response, 'modulators')


def tx_action(action, *args):
    try:
        return action(*args)
    except DomainError as exc:
        # Command failures carry their own certainty from the RF boundary.
        if exc.rf_outcome is None:
            exc.rf_outcome = 'REJECTED'
        raise


@router.post("/modulators/start-all", tags=["modulators"])
@control_route
def start_all(request: Request):
    """Start configured, desired-enabled modulators only; return per-ID status or error."""
    return tx_action(request.app.state.services.modulators.all, True)


@router.post("/modulators/stop-all", tags=["modulators"])
@control_route
def stop_all(request: Request):
    """Attempt every modulator even if one dependency operation fails."""
    # Operational stop preserves persistent desired-enabled flags.
    return tx_action(request.app.state.services.modulators.all, False)


@router.get("/modulators/{id}", response_model=Modulator, tags=["modulators"])
@control_route
def get_modulator(id: int, request: Request):
    return request.app.state.services.modulators.public_get(id)


@router.patch("/modulators/{id}", response_model=Modulator, tags=["modulators"])
@control_route
def update_modulator(id: int, body: ModulatorPatch, request: Request):
    """Persist confirmed-OFF configuration without RF commands; active/uncertain TXs cannot be patched."""
    return request.app.state.services.modulators.update(id, body)


@router.post("/modulators/{id}/start", response_model=ModulatorStatus, tags=["modulators"])
@control_route
def start_modulator(id: int, request: Request):
    """Persist enabled=true and start the TX through the selected Controller client."""
    manager = request.app.state.services.modulators
    return manager.lifecycle_status(tx_action(manager.start, id))


@router.post('/modulators/{id}/save-and-resume', response_model=ModulatorSaveResult, tags=['modulators'])
@control_route
def save_and_resume_modulator(id: int, body: ModulatorPatch, request: Request):
    """Serialize optional STOP, persistence, optional START; report partial outcomes."""
    return request.app.state.services.modulators.save_and_resume(id, body)


@router.post("/modulators/{id}/stop", response_model=ModulatorStatus, tags=["modulators"])
@control_route
def stop_modulator(id: int, request: Request):
    """Persist enabled=false and stop RF before releasing the source."""
    manager = request.app.state.services.modulators
    return manager.lifecycle_status(tx_action(manager.stop, id))


@router.get('/schedules', response_model=list[ScheduleRule], tags=['scheduler'])
@control_route
def list_schedules(request: Request):
    return request.app.state.services.database.list_schedules()


@router.post('/schedules', response_model=ScheduleRule, status_code=201, tags=['scheduler'])
@control_route
def create_schedule(body: ScheduleConfig, request: Request):
    return request.app.state.services.database.save_schedule(body)


@router.patch('/schedules/{id}', response_model=ScheduleRule, tags=['scheduler'])
@control_route
def update_schedule(id: ScheduleId, body: SchedulePatch, request: Request):
    database = request.app.state.services.database
    current = database.get_schedule(id)
    config = ScheduleConfig.model_validate(
        current.model_dump(exclude={'id'}) | body.model_dump(exclude_unset=True))
    return database.save_schedule(config, id)


@router.delete('/schedules/{id}', status_code=204, tags=['scheduler'])
@control_route
def delete_schedule(id: ScheduleId, request: Request):
    request.app.state.services.database.delete_schedule(id)
    return Response(status_code=204)


@router.put("/modulators/{id}/rds/rt", response_model=RtStatus, tags=["runtime RDS"])
@control_route
def set_rt(id: int, body: RtOverride, request: Request):
    """Set runtime-only text; optional TTL uses a monotonic clock."""
    return request.app.state.services.rt.set(id, body)


@router.get("/modulators/{id}/rds/rt", response_model=RtStatus, tags=["runtime RDS"])
@control_route
def get_rt(id: int, request: Request):
    """Inspect effective runtime text, its origin and remaining override TTL."""
    return request.app.state.services.rt.effective(id)


@router.delete("/modulators/{id}/rds/rt", status_code=204, tags=["runtime RDS"])
@control_route
def clear_rt(id: int, request: Request):
    request.app.state.services.rt.clear(id)
    return Response(status_code=204)


@router.get("/status", response_model=HeadendStatus, tags=["system"])
async def status(request: Request):
    return request.app.state.services.status.published_snapshot()


@router.get("/system/config", response_model=SystemConfig, tags=["system"])
@control_route
def get_system_config(request: Request):
    return request.app.state.services.config.get_system()


@router.patch("/system/config", response_model=SystemSaveResult, tags=["system"])
@control_route
def update_system_config(body: SystemConfigPatch, request: Request):
    """Persist settings. Listener/log changes take effect on next daemon startup."""
    return request.app.state.services.config.update_system(body)


@router.get("/system/info", tags=["system"])
async def system_info(request: Request, response: Response):
    return published_view(request, response, 'info')
