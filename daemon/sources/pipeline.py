from typing import Protocol
from ..models.configuration import InputSourceConfig, SourceTestResult, SourceType


class SourcePipeline(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class MockPipeline:
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
