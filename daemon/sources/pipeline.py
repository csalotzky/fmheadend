"""Control-side audio backend contracts; no GI imports or native objects.

GstPipeline implements MediaPipeline structurally, without a wrapper. Minimal
mock factories retain SourcePipeline; manager capability checks also preserve
older injected test doubles. See docs/audio-backend.md for ownership semantics.
"""
from typing import Literal, Protocol, TypedDict, runtime_checkable
from .output import (PcmOutput, DEFAULT_STAGING_BUFFER_MS,
                     DEFAULT_STAGING_MAX_AGE_MS, DEFAULT_CONSUMER_BUFFER_MS)
from ..models.configuration import InputSourceConfig, SourceTestResult
from ..models.audio_profile_file import AudioProfileFile


class RadioTrial(TypedDict):
    token: object
    uri: str
    state: Literal['queued', 'trying', 'pcm', 'failed', 'obsolete']


class RadioRecovery(Protocol):
    """Control-facing URL recovery port, not the ingest retry implementation.

    observe returns a detached cached trial. Tokens are opaque, caller-owned
    identities. offer/accept/discard perform no networking or native calls.
    """
    def observe(self) -> tuple[bool, RadioTrial | None]: ...
    def offer(self, token: object, uri: str) -> bool: ...
    def accept(self, token: object) -> None: ...
    def discard(self, token: object) -> None: ...


class SourcePipeline(Protocol):
    """Minimal injected/mock backend; lifecycle calls remain synchronous."""
    mocked: bool
    source: InputSourceConfig
    # Assigned by SourceManager before publication, not resolved in readers.
    audio_profile_id: str

    def start(self) -> None: ...
    def stop(self) -> None: ...


class MockPipeline:
    mocked = True
    def __init__(self, source: InputSourceConfig):
        self.source = source
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def probe(source: InputSourceConfig) -> SourceTestResult:
    return SourceTestResult(codec="MOCK", sample_rate=48000, channels=2)


@runtime_checkable
class MediaPipeline(SourcePipeline, Protocol):
    """Full production backend. Observations never query native elements.

    start/check_startup may raise SourcePending while retaining ownership.
    stop success permits removal/replacement; an exception retains ownership.
    output is the stable fanout object supplied to a replacement factory.
    No native object, lock, worker thread or completion Event is exposed here.
    """
    output: PcmOutput
    fatal_error: str | None
    radio_url: RadioRecovery | None

    @property
    def startup_pending(self) -> bool: ...

    def check_startup(self) -> None: ...
    def snapshot(self) -> dict: ...
    def pcm_snapshot(self) -> dict: ...
    def request_telemetry(self, enabled: bool, hz: float = 50.0) -> None: ...
    def telemetry_snapshot(self) -> dict: ...
    def probe_result(self) -> SourceTestResult: ...
    def update_source_identity(self, source: InputSourceConfig) -> None:
        """Record a persisted recovery identity; does not reconfigure playback."""
        ...


class MediaPipelineFactory(Protocol):
    """Existing pipeline_factory injection contract for non-mock backends.

    Accept the supplied output by identity. podcast_database_path remains an
    in-process dependency in Phase 1; this is not a future wire schema.
    """
    mocked: bool

    def __call__(self, source: InputSourceConfig, *, profile=None, output=None,
                 timeout_s=None, reconnect=True, telemetry_configured=False,
                 pcm_staging_buffer_ms=DEFAULT_STAGING_BUFFER_MS,
                 pcm_staging_max_age_ms=DEFAULT_STAGING_MAX_AGE_MS,
                 pcm_consumer_buffer_ms=DEFAULT_CONSUMER_BUFFER_MS,
                 podcast_database_path=None) -> MediaPipeline: ...

    def validate_profile(self, profile: AudioProfileFile) -> None: ...
