"""Linux-only test entry point: SIGUSR1 blocks a real Gst streaming callback."""
import argparse
import os
import signal
import socket
import threading

from fmheadendd.sources.audiod import serve
from fmheadendd.sources.gst_pipeline import GstPipeline


class StallingPipeline(GstPipeline):
    def __init__(self, *args, **kwargs):
        self.stall = threading.Event()
        self.never = threading.Event()
        signal.signal(signal.SIGUSR1, lambda *unused: self.stall.set())
        super().__init__(*args, **kwargs)
        signal.signal(signal.SIGUSR2, lambda *unused: self._update(stream_title='event-title'))

    def _sample(self, sink):
        if self.stall.is_set():
            self.never.wait()
        return super()._sample(sink)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--control-fd', type=int)
    parser.add_argument('--pcm-fd', type=int)
    args = parser.parse_args()
    code = 0
    try:
        from contextlib import nullcontext
        with socket.socket(fileno=args.control_fd) as control, (socket.socket(fileno=args.pcm_fd) if args.pcm_fd is not None else nullcontext()) as pcm:
            serve(control, pcm, StallingPipeline)
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    os._exit(code)
