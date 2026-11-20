"""Synthetic LOCAL teardown lock blockage, never production instrumentation."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from contextlib import nullcontext
from fmheadendd.sources.audiod import serve
from fmheadendd.sources.gst_pipeline import GstPipeline


class TeardownPipeline(GstPipeline):
    def mark(self, stage, **extra):
        with (self.trace/str(os.getpid())).open('a') as out:
            out.write(json.dumps(dict(stage=stage, pid=os.getpid(), at=time.monotonic(), **extra))+'\n')

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.trace=Path(os.environ['FMHEADEND_TEST_TEARDOWN_DIR'])
        self.block=threading.Event()
        self.never=threading.Event()
        native_lock=self._uri_change_lock
        owner=self
        class ObservedLock:
            def acquire(self,*a,**kw):return native_lock.acquire(*a,**kw)
            def release(self):return native_lock.release()
            def __enter__(self):
                if owner.block.is_set():owner.mark('lock-wait',caller=sys._getframe(1).f_code.co_name)
                native_lock.acquire()
                return self
            def __exit__(self,*unused):native_lock.release()
        self._uri_change_lock=ObservedLock()
        def arm(*unused):
            signal.signal(signal.SIGTERM,signal.SIG_IGN)
            self.block.set()
            self.mark('armed-term-ignore')
        signal.signal(signal.SIGUSR1,arm)

    def _sample(self,sink):
        if self.block.is_set() and self._uri_change_lock.acquire(blocking=False):
            self.mark('streaming-callback-holds-uri-lock')
            self.never.wait()
        return super()._sample(sink)

    def stop(self):
        self.mark('native-stop-entered')
        return super().stop()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--control-fd',type=int);parser.add_argument('--pcm-fd',type=int);args=parser.parse_args()
    code=0
    try:
        with socket.socket(fileno=args.control_fd) as control,(socket.socket(fileno=args.pcm_fd) if args.pcm_fd is not None else nullcontext()) as pcm:
            serve(control,pcm,TeardownPipeline)
    except Exception:
        import traceback;traceback.print_exc();code=1
    os._exit(code)
