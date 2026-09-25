"""Isolated real decoder -> Source Gain -> JSON DSP -> PCM fan-out test.

Temporary localhost fixtures only; no mux, device, control client or RF access.
"""
import functools
import json
import math
import resource
import struct
import tempfile
import threading
import time
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.dsp import neutral_stages
from fmheadendd.sources.gst_pipeline import GstPipeline


def pcm(pipeline):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        data=pipeline.output.read(0)
        if data: return struct.unpack('<'+'h'*(len(data)//2),data)
        time.sleep(.005)
    raise AssertionError('No PCM at fan-out')


results=[]
with tempfile.TemporaryDirectory(prefix='fmheadend-dsp-source-') as directory:
    path=Path(directory)
    with wave.open(str(path/'source.wav'),'wb') as wav:
        wav.setparams((1,2,44100,0,'NONE','not compressed'))
        wav.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(i*2*math.pi*1000/44100))) for i in range(44100*4)))
    handler=functools.partial(SimpleHTTPRequestHandler,directory=directory)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        for builtin in (False,True):
            stages=neutral_stages()
            for s in stages.values():
                if s['selected'] not in ('VOLUME','TRIM'): s['selected']='OFF'
            stages['input_gain']['implementations']['VOLUME']['gain_db']=-6
            file=path/'profile.json'
            file.write_text(json.dumps(dict(schema_version=2,name='Shared test',stages=stages)))
            profile=AudioProfileFile.model_validate_json(file.read_text()) if builtin else None
            source=InputSourceConfig(name='Isolated',type='HTTP',config={'uri':f'http://127.0.0.1:{server.server_port}/source.wav'},
                input_gain_db=-6,processor_mode='builtin' if builtin else 'bypass',
                audio_profile_path='custom/probe.json' if builtin else None)
            output=None
            for rebuild in range(2):
                pipeline=GstPipeline(source,profile=profile,output=output,reconnect=False)
                pipeline.output.attach(0);pipeline.output.attach(1)
                started=time.monotonic()
                try:
                    pipeline.start()
                    startup=time.monotonic()-started
                    samples=pcm(pipeline)
                    # Final audioconvert dithers S16 channels independently.
                    assert max(abs(samples[i]-samples[i+1]) for i in range(0,len(samples),2)) <= 2
                    peak=max(abs(x) for x in samples)
                    expected=8000*10**((-12 if builtin else -6)/20)
                    assert abs(peak-expected)<expected*.04,(peak,expected)
                    assert pipeline.output.read(1)
                    snapshot=pipeline.snapshot()
                    assert all(x in snapshot['output_caps'] for x in ['S16LE','48000','channels=(int)2'])
                    results.append(dict(builtin=builtin,rebuild=rebuild,startup_s=startup,peak=peak,caps=snapshot['output_caps']))
                    output=pipeline.output
                finally: pipeline.stop()
    finally:
        server.shutdown();server.server_close();thread.join()
print(json.dumps(dict(results=results,max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),indent=2))
