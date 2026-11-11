"""Deterministic audiod process fixture; no native decoder or network playback."""
import argparse
import os
import socket
import threading

from fmheadendd.models.configuration import SourceTestResult
from fmheadendd.errors import SourceUnavailable
from fmheadendd.sources.audiod import serve
from fmheadendd.sources.output import PCM_CAPS

PCM = b'\x00\x10\x00\x10' * 960


class FakeDecoder:
    fatal_error = None

    def __init__(self, source, *, output, **options):
        self.source, self.output = source, output
        self.closed = threading.Event()

    def start(self):
        if self.source.name == 'source-error':
            raise SourceUnavailable('Fixture HTTP error')
        if self.source.name == 'hang-start':
            self.closed.wait(60)
        if self.source.name == 'crash':
            os._exit(23)

        def pump():
            while not self.closed.wait(0.02):
                self.output.publish(PCM)
        threading.Thread(target=pump, daemon=True).start()

    def stop(self):
        if self.source.name == 'hang-stop':
            self.closed.wait(60)
        self.closed.set()

    def snapshot(self):
        return dict(state='RUNNING', stream_title=self.source.name, output_caps=PCM_CAPS)

    def probe_result(self):
        return SourceTestResult(ok=True, mocked=False, codec='fixture',
                                output_caps=PCM_CAPS, message='Synthetic PCM')

    def pcm_snapshot(self):
        return dict(staging={})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--control-fd', type=int)
    parser.add_argument('--pcm-fd', type=int)
    args = parser.parse_args()
    code = 0
    try:
        with socket.socket(fileno=args.control_fd) as control, socket.socket(fileno=args.pcm_fd) as pcm:
            serve(control, pcm, FakeDecoder)
    except Exception:
        code = 1
    os._exit(code)
