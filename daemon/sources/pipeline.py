from typing import Protocol
from .output import PcmOutput
from ..models.configuration import InputSourceConfig, SourceTestResult, SourceType


class SourcePipeline(Protocol):
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
    if source.type == SourceType.ANALOG:
        return SourceTestResult(message="Mock analog input; no pipeline or hardware probe.")
    return SourceTestResult(codec="MOCK", sample_rate=48000, channels=2)


class MediaPipeline(SourcePipeline, Protocol):
    """Additional contract supplied by real ingest implementations."""
    mocked: bool
    output: PcmOutput

    def snapshot(self) -> dict: ...
    def probe_result(self) -> SourceTestResult: ...
