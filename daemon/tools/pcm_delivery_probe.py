"""Isolated single-source decoder -> staging -> fanout measurements, no mux/RP IO.

Run from an isolated package workspace, optionally --profile /read-only/profile.json.
Temporary localhost MP3 fixture and one sequential pipeline per mode only.
"""
import argparse
from collections import Counter
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time

from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.dsp import neutral_stages
from fmheadendd.sources.gst_pipeline import GstPipeline, load_gst


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    Gst = load_gst()
    profile = (AudioProfileFile.model_validate_json(args.profile.read_text()) if args.profile else
               AudioProfileFile(schema_version=1, name='Probe filters', stages=neutral_stages()))
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    with tempfile.TemporaryDirectory(prefix='pcm-probe-') as directory:
        fixture = Path(directory) / 'test.mp3'
        encoder = Gst.parse_launch(f'audiotestsrc num-buffers=160 samplesperbuffer=2400 ! '
            'audio/x-raw,rate=48000,channels=2 ! audioconvert ! lamemp3enc target=bitrate bitrate=128 cbr=true ! '
            f'filesink location="{fixture}"')
        try:
            encoder.set_state(Gst.State.PLAYING)
            msg = encoder.get_bus().timed_pop_filtered(10*Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            assert msg and msg.type == Gst.MessageType.EOS
        finally:
            encoder.set_state(Gst.State.NULL)
        server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=directory))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for builtin in (False, True):
                source = InputSourceConfig(name='Probe', type='HTTP', config={'uri': f'http://127.0.0.1:{server.server_port}/test.mp3'},
                    processor_mode='builtin' if builtin else 'bypass', audio_profile_path='custom/probe.json' if builtin else None)
                pipeline = GstPipeline(source, profile=profile if builtin else None, reconnect=False)
                pipeline.output.attach(0)
                sizes, arrivals, delays, pending = Counter(), [], [], {}
                put, publish = pipeline.staging.put, pipeline.output.publish
                def record(data, **kwargs):
                    now = time.monotonic(); sizes[len(data)] += 1; arrivals.append(now); pending[id(data)] = now
                    return put(data, **kwargs)
                def forward(data, **kwargs):
                    then = pending.pop(id(data), None)
                    if then is not None: delays.append(time.monotonic()-then)
                    return publish(data, **kwargs)
                pipeline.staging.put, pipeline.output.publish = record, forward
                try:
                    pipeline.start()
                    until = time.monotonic()+4
                    while time.monotonic() < until:
                        pipeline.output.read(0)
                        time.sleep(.001)
                    gaps = [b-a for a,b in zip(arrivals, arrivals[1:])]
                    print(json.dumps(dict(builtin=builtin, buffers=dict(sizes),
                        mean_callback_ms=1000*sum(gaps)/len(gaps), max_callback_ms=1000*max(gaps),
                        max_staging_ms=1000*max(delays), mean_staging_ms=1000*sum(delays)/len(delays),
                        staging_dropped=pipeline.staging.dropped,
                        pcm=pipeline.pcm_snapshot()), indent=2))
                finally:
                    pipeline.stop()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': main()
