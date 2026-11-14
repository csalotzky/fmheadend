"""Test-only RNG injection and PCM threshold trace; never used by CPU runs."""
import argparse,json,os,random,socket,time
from contextlib import nullcontext
from collections import deque

from fmheadendd.sources.audiod import serve
from fmheadendd.tests.audiod_local_direct_fixture import TracePipeline


class PodcastTracePipeline(TracePipeline):
    def __init__(self,*args,**kwargs):
        original=kwargs['podcast_runtime_factory'];self.history_events=deque(maxlen=4);self.builds=0
        def playback(*a,**kw):
            runtime=original(*a,**kw)
            # Keys are hashed episode identities, not titles/guids. Seed the
            # existing scheduler against the actual first complete catalog.
            seeded=[False]
            def seed_catalog(catalog):
                episodes=[e for f in catalog.feeds for e in f.episodes]
                if seeded[0] or len(episodes)!=3:return
                desired=[e.key for e in sorted(episodes,key=lambda e:e.title)]
                for seed in range(100):
                    bag=sorted(e.key for e in episodes);rng=random.Random(seed);rng.shuffle(bag)
                    if bag[::-1]==desired:break
                else:raise AssertionError('No deterministic three-episode test seed')
                runtime.scheduler.rng.seed(seed);seeded[0]=True
            apply=runtime.apply_snapshot
            def snapshot(value):
                seed_catalog(value.catalog);apply(value)
            runtime.apply_snapshot=snapshot;seed_catalog(runtime.catalog_view)
            pcm=runtime.pcm
            def count(size=0):
                ticket=runtime.rendering;marked=ticket.marked if ticket else False
                before=ticket.pcm_bytes if ticket else 0
                pcm(size)
                if ticket and not marked and ticket.marked:
                    episode=ticket.context
                    self.history_events.append(dict(kind='history',at=time.monotonic_ns(),
                        before=before,bytes=ticket.pcm_bytes,size=size,episode_id=episode.episode_id,
                        count=runtime.history_state[episode.key].play_count))
            runtime.pcm=count
            return runtime
        kwargs['podcast_runtime_factory']=playback
        super().__init__(*args,**kwargs)
        writer=getattr(self.output,'writer',None)
        if writer is not None:
            original_frame=writer.frame_sent
            def frame(channel,data):
                original_frame(channel,data)
                events=[]
                while self.history_events:events.append(self.history_events.popleft())
                if events:
                    with self.trace.open('a') as f:
                        for event in events:f.write(json.dumps(event)+'\n')
            writer.frame_sent=frame

    def _build(self):
        self.builds+=1
        with self.trace.open('a') as f:f.write(json.dumps(dict(kind='build',count=self.builds))+'\n')
        return super()._build()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--control-fd',type=int);parser.add_argument('--pcm-fd',type=int);args=parser.parse_args()
    code=0
    try:
        with socket.socket(fileno=args.control_fd) as c,(socket.socket(fileno=args.pcm_fd) if args.pcm_fd is not None else nullcontext()) as p:serve(c,p,PodcastTracePipeline)
    except Exception:
        import traceback;traceback.print_exc();code=1
    os._exit(code)
