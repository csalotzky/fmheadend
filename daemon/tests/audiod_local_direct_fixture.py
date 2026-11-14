"""Test-only first-frame trace. No tracing is enabled in CPU measurements."""
import argparse,json,os,socket,time
from contextlib import nullcontext
from pathlib import Path
from fmheadendd.sources.audiod import serve
from fmheadendd.tests.audiod_native_stall_fixture import StallingPipeline


class TracePipeline(StallingPipeline):
    def __init__(self,*a,**kw):
        self.handoffs=[]
        super().__init__(*a,**kw)
        self.trace=Path(os.environ['FMHEADEND_TEST_LOCAL_TRACE'])
        writer=getattr(self.output,'writer',None)
        if writer is not None:
            original=writer.frame_sent;last=[None]
            def frame(channel,data):
                original(channel,data)
                title=data.status.get('title')
                if title!=last[0]:
                    last[0]=title
                    with self.trace.open('a') as f:f.write(json.dumps(dict(kind='frame',at=time.monotonic_ns(),produced=data.produced_ns,title=title,prefix=data[:4].hex(),handoffs=list(self.handoffs)))+'\n')
            writer.frame_sent=frame

    def _local_handoff(self,*args):
        # Cached scalar observation only in about-to-finish; no socket/file IO.
        self.handoffs.append((time.monotonic_ns(),self.snapshot().get('title')))
        self.handoffs[:]=self.handoffs[-8:]
        return super()._local_handoff(*args)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--control-fd',type=int);parser.add_argument('--pcm-fd',type=int);args=parser.parse_args()
    code=0
    try:
        with socket.socket(fileno=args.control_fd) as c,(socket.socket(fileno=args.pcm_fd) if args.pcm_fd is not None else nullcontext()) as p:serve(c,p,TracePipeline)
    except Exception:
        import traceback;traceback.print_exc();code=1
    os._exit(code)
