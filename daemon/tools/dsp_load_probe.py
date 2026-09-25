"""Operator-authorized, bounded 8 independent MP3 decoder/DSP test.

No mux/device/control connection. Runs sequential six-second measurement windows,
using localhost fixtures, with all workers closed in finally paths.
"""
import functools
import json
import os
from pathlib import Path
import resource
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.dsp import neutral_stages
from fmheadendd.sources.gst_pipeline import GstPipeline, load_gst

Gst=load_gst()


def cpu_ticks():
    result={}
    for line in Path('/proc/stat').read_text().splitlines():
        p=line.split()
        if p[0].startswith('cpu'):
            v=list(map(int,p[1:9]));result[p[0]]=(sum(v),v[3]+v[4])
    return result


def cpu_percent(a,b):
    return {k:round(100*(1-(b[k][1]-v[1])/(b[k][0]-v[0])),1) for k,v in a.items()}


def available_kib():
    return int(next(l for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')).split()[1])


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass


results=[]
with tempfile.TemporaryDirectory(prefix='fmheadend-dsp-load-') as directory:
    folder=Path(directory)
    # Eight distinct tones, encoded independently; never the shared-source fanout shortcut.
    for i in range(8):
        pipe=Gst.parse_launch(f'audiotestsrc num-buffers=600 samplesperbuffer=2400 freq={220+i*173} volume=0.2 ! '
            'audio/x-raw,rate=48000,channels=2 ! audioconvert ! lamemp3enc target=bitrate bitrate=128 cbr=true ! '
            f'filesink location="{folder / (str(i)+".mp3")}"')
        try:
            pipe.set_state(Gst.State.PLAYING)
            msg=pipe.get_bus().timed_pop_filtered(10*Gst.SECOND,Gst.MessageType.EOS|Gst.MessageType.ERROR)
            assert msg and msg.type==Gst.MessageType.EOS
        finally: pipe.set_state(Gst.State.NULL)
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=directory))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        for heavy in (False,True):
            pipelines=[]
            stages=neutral_stages()
            stages['agc']['selected']='LSP_AGC';stages['limiter']['selected']='LSP_LIMITER'
            stages['eq']['selected']='LSP_EQ' if heavy else 'OFF'
            stages['wideband']['selected']='LSP_COMPRESSOR' if heavy else 'OFF'
            stages['multiband']['selected']='LSP_MULTIBAND' if heavy else 'OFF'
            if heavy:
                for band in stages['multiband']['implementations']['LSP_MULTIBAND']['bands']: band['ratio']=4.
            profile=AudioProfileFile.model_validate(dict(schema_version=1,name='Load',stages=stages))
            began=time.monotonic()
            try:
                for i in range(8):
                    source=InputSourceConfig(name=f'Independent {i}',type='HTTP',
                        config={'uri':f'http://127.0.0.1:{server.server_port}/{i}.mp3'},
                        processor_mode='builtin',audio_profile_path='custom/load.json')
                    p=GstPipeline(source,profile=profile,reconnect=False);pipelines.append(p)
                    p.output.attach(0);p.start()
                startup=time.monotonic()-began
                # Drain launch backlog before measuring steady-state delivery.
                for p in pipelines: p.output.clear()
                counts=[0]*8; gaps=[0.]*8;last=[time.monotonic()]*8
                drops=[p.staging.dropped for p in pipelines]
                ticks=cpu_ticks();cpu=time.process_time();start=time.monotonic()
                while time.monotonic()-start<6:
                    now=time.monotonic()
                    for i,p in enumerate(pipelines):
                        data=p.output.read(0)
                        if data:
                            counts[i]+=len(data);gaps[i]=max(gaps[i],now-last[i]);last[i]=now
                        assert p.snapshot()['state']=='RUNNING',p.snapshot()
                    if available_kib()<96*1024: raise RuntimeError('Stopping: less than 96 MiB available')
                    time.sleep(.002)
                duration=time.monotonic()-start
                result=dict(case='four-band + EQ + wideband + AGC + limiter' if heavy else 'AGC + limiter',
                    streams=8,measurement_s=duration,startup_all_s=startup,host_cpu_percent=cpu_percent(ticks,cpu_ticks()),
                    process_cpu_percent=100*(time.process_time()-cpu)/duration,
                    max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,mem_available_kib=available_kib(),
                    pcm_bytes=counts,max_delivery_gap_ms=[round(v*1000,2) for v in gaps],
                    staging_drops=[p.staging.dropped-d for p,d in zip(pipelines,drops)])
                # Rebuild one source with its output object while seven stay running.
                old=pipelines[0];started=time.monotonic();old.stop()
                new=GstPipeline(old.source,profile=profile,output=old.output,reconnect=False)
                pipelines[0]=new;new.start()
                result['one_source_rebuild_s']=time.monotonic()-started
                result['other_sources_running']=all(p.snapshot()['state']=='RUNNING' for p in pipelines[1:])
                results.append(result)
            finally:
                for p in pipelines: p.stop()
    finally:
        server.shutdown();server.server_close();thread.join()
print(json.dumps(results,indent=2))
