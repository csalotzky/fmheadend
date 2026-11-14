"""Real child Radio Browser retry and native peak telemetry over direct PCM."""
import sys, wave
import pytest
from fmheadendd.tests.test_gstreamer import gst,http_files,require,file_profile
from fmheadendd.tests.test_audio_worker import until,finish
from fmheadendd.tests.test_local_direct_native import SocketSink
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.errors import SourcePending

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native worker sockets')


def test_radio_candidate_pcm_and_real_telemetry(gst,http_files,tmp_path):
    require(gst,'souphttpsrc','wavparse','level')
    folder,url=http_files
    with wave.open(str(folder/'live.wav'),'wb') as out:
        out.setparams((2,2,48000,0,'NONE',''));out.writeframes(b'\x00\x10\x00\x20'*48000*40)
    s=InputSourceConfig(name='Radio',type='HTTP',config=dict(uri=url+'/missing.wav',
        radio_browser_uuid='960e57c5-0601-11e8-ae97-52543be04c81'))
    sink=SocketSink(tmp_path)
    p=ProcessPipeline(s,profile=file_profile('radio'),direct_runtime=tmp_path,telemetry_configured=True)
    try:
        p.request_telemetry(True,20)
        try:p.start()
        except SourcePending:pass
        p.set_output_lease(1,1)
        until(lambda:p.radio_url.observe()[0],timeout=35)
        token=object();assert p.radio_url.offer(token,url+'/live.wav')
        until(lambda: p.radio_url.observe()[1] and p.radio_url.observe()[1]['state']=='pcm',timeout=20)
        until(lambda:len(sink.records)>3,timeout=5)
        until(lambda:p.telemetry_snapshot()['telemetry_available'],timeout=10)
        sample=p.telemetry_snapshot()
        assert sample['input'] is not None and sample['output'] is not None
        assert sample['sample_monotonic_ns']>0
        p.radio_url.accept(token)
        p.request_telemetry(False)
        assert not p.telemetry_snapshot()['telemetry_available']
    finally:
        finish(p);sink.close()
