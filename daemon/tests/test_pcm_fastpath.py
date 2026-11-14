"""PCM framing/caching must preserve bytes, identity, timing and fencing."""
import json
import socket
import struct
import threading
import time

import pytest

from fmheadendd.sources import audio_ipc as ipc
from fmheadendd.sources import process_pipeline as backend
from fmheadendd.sources.output import PCM_CAPS
from fmheadendd.tests.test_audio_worker import adapter, pcm_message


def packet(encoder, sequence=1, title='rendering', pcm=b'\x01\x00\x02\x00', produced=None):
    return encoder.pack(sequence, produced or time.monotonic_ns(),
        dict(state='RUNNING', title=title, output_caps=PCM_CAPS),
        dict(ok=True, mocked=False), pcm)


def test_fast_pcm_roundtrip_owned_bytes_and_complete_metadata_after_drop():
    encoder=ipc.PcmEncoder(7,'a'*32);decoder=ipc.PcmDecoder()
    packet(encoder,1)  # Lost initial packet must not lose metadata for the next.
    wire=packet(encoder,2)
    body=wire[4:]
    message,pcm=decoder.unpack(body)
    assert struct.unpack('!I',wire[:4])[0]==len(body)
    assert (message['source_id'],message['generation'],message['sequence'])==(7,'a'*32,2)
    assert message['payload']['status']['title']=='rendering'
    assert pcm==b'\x01\x00\x02\x00' and type(pcm) is bytes


def test_tiny_pcm_does_not_retain_large_metadata_in_consumer_buffer():
    encoder=ipc.PcmEncoder(1,'a'*32)
    wire=packet(encoder,title='x'*60000)
    message,pcm=ipc.PcmDecoder().unpack(wire[4:])
    assert len(message['payload']['status']['title'])==60000
    assert type(pcm) is bytes and len(pcm)==4


def test_fast_pcm_encodes_and_decodes_metadata_only_on_change(monkeypatch):
    dumps,loads=ipc.json.dumps,ipc.json.loads
    calls={'encode':0,'decode':0}
    def encode(*a,**kw):calls['encode']+=1;return dumps(*a,**kw)
    def decode(*a,**kw):calls['decode']+=1;return loads(*a,**kw)
    monkeypatch.setattr(ipc.json,'dumps',encode);monkeypatch.setattr(ipc.json,'loads',decode)
    encoder=ipc.PcmEncoder(1,'a'*32);decoder=ipc.PcmDecoder()
    for n in range(1,21):
        msg,data=decoder.unpack(packet(encoder,n,title='old' if n<20 else 'new')[4:])
        assert msg['sequence']==n and msg['payload']['status']['title']==('old' if n<20 else 'new')
    assert calls=={'encode':2,'decode':2}


def test_fast_pcm_mutated_caller_snapshot_cannot_reuse_old_encoding():
    encoder=ipc.PcmEncoder(1,'a'*32)
    status={'state':'RUNNING','title':'old'}
    first=encoder.pack(1,1,status,{},b'\0'*4)
    status['title']='new'
    second=encoder.pack(2,2,status,{},b'\0'*4)
    assert ipc.unpack(first[4:])[0]['payload']['status']['title']=='old'
    assert ipc.unpack(second[4:])[0]['payload']['status']['title']=='new'


def test_fast_pcm_fragmented_and_coalesced_stream_is_exactly_ordered():
    encoder=ipc.PcmEncoder(1,'a'*32);decoder=ipc.PcmDecoder()
    frames=[packet(encoder,n,pcm=bytes([n])*256) for n in range(1,5)]
    a,b=socket.socketpair();ipc.configure(b)
    def send():
        for byte in frames[0]:a.sendall(bytes([byte]))
        a.sendall(b''.join(frames[1:]))
    thread=threading.Thread(target=send)
    try:
        thread.start()
        for n in range(1,5):
            msg,data=ipc.receive(b,pcm_decoder=decoder)
            assert msg['sequence']==n and data==bytes([n])*256
    finally:thread.join(1);a.close();b.close()


@pytest.mark.parametrize('mutation',['sequence','timestamp','metadata-size','alignment','json','format'])
def test_fast_pcm_rejects_malformed_record(mutation,adapter):
    encoder=ipc.PcmEncoder(adapter.source_id,adapter.generation)
    body=bytearray(packet(encoder)[4:])
    if mutation in ('sequence','timestamp','metadata-size'):
        fields=list(ipc._PCM_HEADER.unpack_from(body))
        fields[{'sequence':3,'timestamp':4,'metadata-size':5}[mutation]]=0
        ipc._PCM_HEADER.pack_into(body,0,*fields)
    elif mutation=='alignment':body.append(0)
    elif mutation=='json':body[ipc._PCM_HEADER.size]=255
    else:
        body=bytes(body).replace(b'audio/x-raw',b'audio/x-bad')
    with pytest.raises((ipc.ProtocolError,ValueError)):
        message,pcm=ipc.PcmDecoder().unpack(body)
        adapter._accept('pcm',message,pcm)
    assert not adapter._has_pcm


def test_fast_pcm_cached_models_still_fence_every_packet(adapter,monkeypatch):
    original=backend.InputSourceStatus;calls=[]
    def validate(*a,**kw):calls.append(kw);return original(*a,**kw)
    monkeypatch.setattr(backend,'InputSourceStatus',validate)
    encoder=ipc.PcmEncoder(adapter.source_id,adapter.generation);decoder=ipc.PcmDecoder()
    adapter.output.attach(0)
    for n in (1,2):adapter._accept('pcm',*decoder.unpack(packet(encoder,n)[4:]))
    assert len(calls)==1 and adapter.output.read(0) and adapter.output.read(0)
    adapter._accept('pcm',*decoder.unpack(packet(encoder,2)[4:]))
    other=ipc.PcmEncoder(adapter.source_id,'b'*32)
    adapter._accept('pcm',*decoder.unpack(packet(other,3)[4:]))
    adapter._accept('pcm',*decoder.unpack(packet(encoder,4,produced=time.monotonic_ns()-1_000_000_000)[4:]))
    assert adapter.output.read(0) is None
    assert adapter.stale_messages==2 and adapter.stale_pcm==1 and len(calls)==1
    adapter._accept('pcm',*decoder.unpack(packet(encoder,5,title='new')[4:]))
    assert len(calls)==2 and adapter.snapshot()['title']=='new' and adapter.output.read(0)


def test_fast_pcm_partial_eof_is_not_a_record():
    a,b=socket.socketpair();ipc.configure(b)
    wire=packet(ipc.PcmEncoder(1,'a'*32))
    try:
        a.sendall(wire[:-1]);a.shutdown(socket.SHUT_WR)
        with pytest.raises(EOFError):ipc.receive(b,pcm_decoder=ipc.PcmDecoder())
    finally:a.close();b.close()


def test_worker_probe_cache_tracks_rendered_identity_and_icy_station(monkeypatch):
    from types import SimpleNamespace
    from fmheadendd.sources.gst_pipeline import GstPipeline
    pipeline=GstPipeline.__new__(GstPipeline)
    pipeline.source=SimpleNamespace(type='HTTP')
    pipeline._lock=threading.Lock();pipeline._station_name='station one'
    info=dict(state='RUNNING',bitrate_bps=128000,codec='MP3',input_sample_rate=44100,
        input_channels=2,output_caps=PCM_CAPS,stream_title='rendered old',last_error=None)
    original=pipeline._probe_from_snapshot;calls=[]
    def build(*a):calls.append(a);return original(*a)
    monkeypatch.setattr(pipeline,'_probe_from_snapshot',build)
    first=pipeline.transport_probe(info)
    assert pipeline.transport_probe(dict(info)) is first and len(calls)==1
    assert first==original(info,'station one').model_dump(mode='json')
    pipeline._station_name='station two'
    assert pipeline.transport_probe(info)['station_name']=='station two' and len(calls)==2
    newer=dict(info,stream_title='newly rendered')
    assert pipeline.transport_probe(newer)['stream_title']=='newly rendered'
    # An older queued PCM block must retain its own identity even after refresh.
    assert pipeline.transport_probe(info)['stream_title']=='rendered old'
    assert pipeline.transport_probe(dict(info,state='CONNECTING'))['ok'] is False


def test_first_worker_pcm_has_validated_caps_before_public_state_update():
    from fmheadendd.sources.gst_pipeline import GstPipeline
    from fmheadendd.sources.audiod import TransportOutput
    pipeline=GstPipeline.__new__(GstPipeline)
    pipeline._lock=threading.Lock();pipeline._station_name=None
    from types import SimpleNamespace
    pipeline.source=SimpleNamespace(type='ALSA')
    pipeline._snapshot=dict(state='CONNECTING',output_caps=None,bitrate_bps=None,codec=None,
        input_sample_rate=None,input_channels=None,stream_title=None,last_error=None)
    pipeline._caps='audio/x-raw, format=(string)S16LE, rate=(int)48000, channels=(int)2, layout=(string)interleaved'
    output=TransportOutput(1,'a'*32);output.pipeline=pipeline
    output.publish(b'\0'*16)
    message,pcm=ipc.unpack(output.queue.pop()[4:])
    assert message['payload']['status']['output_caps']==pipeline._caps and len(pcm)==16
    assert message['payload']['probe']['output_caps']==pipeline._caps
    assert pipeline.snapshot()['output_caps'] is None  # Default lifecycle cache unchanged.


def test_pcm_age_starts_before_snapshot_and_probe_work(monkeypatch):
    from types import SimpleNamespace
    from fmheadendd.sources import audiod
    from fmheadendd.models.configuration import SourceTestResult
    clock=[1_000_000_000]
    monkeypatch.setattr(audiod.time,'monotonic_ns',lambda:clock[0])
    def snapshot():
        clock[0]+=200_000_000
        return {'state':'RUNNING','output_caps':PCM_CAPS}
    output=audiod.TransportOutput(1,'a'*32)
    output.pipeline=SimpleNamespace(snapshot=snapshot,probe_result=lambda:SourceTestResult(ok=True,mocked=False))
    output.publish(b'\0'*4)
    message,_=ipc.unpack(output.queue.pop()[4:])
    assert message['payload']['produced_ns']==1_000_000_000
