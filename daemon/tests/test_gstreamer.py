"""Real native tests. Missing GI/plugins skip explicitly; runtime failures fail."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import socket
import threading
import time
import wave

import pytest
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline, load_gst, caps_structure, make_rtp_caps, rtp_parameters

from fmheadendd.models.audio_profile_file import AudioProfileFile

def file_profile(name, gain_db=0):
    from fmheadendd.models.dsp import neutral_stages, STAGES
    stages = neutral_stages()
    for key, (_, choices) in STAGES.items():
        if 'OFF' in choices: stages[key]['selected'] = 'OFF'
    stages['input_gain']['implementations']['VOLUME']['gain_db'] = gain_db
    return AudioProfileFile.model_validate(dict(schema_version=1, name=name, stages=stages))

pytestmark = pytest.mark.gstreamer


@pytest.fixture
def gst():
    try:
        return load_gst()
    except DependencyUnavailable as exc:
        pytest.skip(str(exc))


def require(gst, *factories):
    missing = [name for name in factories if gst.ElementFactory.find(name) is None]
    if missing:
        pytest.skip("Missing GStreamer plugins: " + ", ".join(missing))


@pytest.fixture
def http_files(tmp_path):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tmp_path, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def config(kind, values):
    return InputSourceConfig(name="integration", type=kind, config=values)


def assert_pcm(pipeline, gst):
    # Normal start now confirms construction; only an explicit probe waits for PCM.
    assert pipeline._ready.wait(pipeline.timeout_s), pipeline.snapshot()
    result = pipeline.probe_result()
    assert result.ok and not result.mocked, result.message
    caps = gst.Caps.from_string(result.output_caps)
    with caps_structure(caps) as structure:
        assert structure.get_name() == "audio/x-raw"
        assert structure.get_string("format") == "S16LE"
        assert structure.get_value("rate") == 48000
        assert structure.get_value("channels") == 2
        assert structure.get_string("layout") == "interleaved"
    assert pipeline.sink.get_static_pad("sink").get_current_caps().is_equal(caps)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        pcm = pipeline.output.read(0)
        if pcm:
            assert len(pcm) % 4 == 0
            return pcm
        time.sleep(0.01)
    pytest.fail("No PCM reached the output consumer")


def test_http_resamples_mono_44100_to_exact_caps(gst, http_files):
    require(gst, "souphttpsrc", "wavparse")
    folder, url = http_files
    with wave.open(str(folder / "test.wav"), "wb") as wav:
        wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 44100 * 20)
    pipeline = GstPipeline(config("HTTP", {"uri": url + "/test.wav"}), reconnect=False)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        assert_pcm(pipeline, gst)
        assert pipeline.snapshot()["input_sample_rate"] == 44100
        assert pipeline.snapshot()["input_channels"] == 1
    finally:
        pipeline.stop()


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("mode", ["UDP", "PCM", "OPUS", "MP3", "AAC"])
def test_loopback_udp_rtp(gst, mode):
    port = free_udp_port()
    if mode == "UDP":
        values = {"host": "127.0.0.1", "port": port, "format": "S16LE", "sample_rate": 44100, "channels": 1}
        sender = "audiotestsrc is-live=true ! audio/x-raw,format=S16LE,rate=44100,channels=1"
        kind = "UDP"
    else:
        chains = {
            "PCM": ("rtpL16pay", "audioconvert ! audio/x-raw,format=S16BE,rate=44100,channels=1 ! rtpL16pay pt=96"),
            "OPUS": ("opusenc", "audioconvert ! audioresample ! opusenc ! rtpopuspay pt=96"),
            "MP3": ("lamemp3enc", "audioconvert ! lamemp3enc ! mpegaudioparse ! rtpmpapay pt=96"),
            "AAC": ("avenc_aac", "audioconvert ! audio/x-raw,rate=48000,channels=2 ! avenc_aac ! aacparse ! rtpmp4gpay pt=96"),
        }
        require(gst, chains[mode][0])
        values = {"host": "127.0.0.1", "port": port, "codec": mode, "payload_type": 96}
        if mode == "PCM":
            values.update(clock_rate=44100, channels=1)
        if mode == "AAC":
            values.update(clock_rate=48000, channels=2, codec_data="1190")
        sender = "audiotestsrc is-live=true ! " + chains[mode][1]
        kind = "RTP"
    native_sender = gst.parse_launch(sender + f" ! udpsink host=127.0.0.1 port={port} sync=true")
    pipeline = GstPipeline(config(kind, values), timeout_s=5)
    pipeline.output.attach(0)
    try:
        native_sender.set_state(gst.State.PLAYING)
        pipeline.start()
        assert_pcm(pipeline, gst)
    finally:
        pipeline.stop()
        native_sender.set_state(gst.State.NULL)


def render(gst, launch):
    pipeline = gst.parse_launch(launch)
    try:
        pipeline.set_state(gst.State.PLAYING)
        result = pipeline.get_bus().timed_pop_filtered(20 * gst.SECOND, gst.MessageType.ERROR | gst.MessageType.EOS)
        assert result is not None, "Fixture generation timed out"
        if result.type == gst.MessageType.ERROR:
            pytest.fail(result.parse_error()[0].message)
    finally:
        pipeline.set_state(gst.State.NULL)


def test_hls_local_fixture(gst, http_files):
    require(gst, "avenc_aac", "aacparse", "mpegtsmux", "hlssink", "souphttpsrc")
    folder, url = http_files
    render(gst, 'audiotestsrc num-buffers=900 ! audioconvert ! avenc_aac ! aacparse ! mpegtsmux ! '
           f'hlssink target-duration=1 playlist-length=0 max-files=0 location="{folder.as_posix()}/seg%05d.ts" '
           f'playlist-location="{folder.as_posix()}/index.m3u8"')
    pipeline = GstPipeline(config("HTTP", {"uri": url + "/index.m3u8"}), timeout_s=10, reconnect=False)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        assert_pcm(pipeline, gst)
    finally:
        pipeline.stop()


def test_icy_title_metadata(gst, tmp_path):
    require(gst, "lamemp3enc", "souphttpsrc", "icydemux")
    audio = tmp_path / "radio.mp3"
    render(gst, f'audiotestsrc num-buffers=900 ! audioconvert ! lamemp3enc ! filesink location="{audio.as_posix()}"')
    data = audio.read_bytes()
    class IcyHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("icy-metaint", "4096")
            self.end_headers()
            title = b"StreamTitle='Integration Artist - Integration Title';"
            metadata = title.ljust(((len(title) + 15) // 16) * 16, b"\0")
            try:
                for offset in range(0, len(data), 4096):
                    block = data[offset:offset + 4096]
                    self.wfile.write(block)
                    if len(block) == 4096:
                        self.wfile.write(bytes([len(metadata) // 16]) + metadata)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), IcyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pipeline = GstPipeline(config("HTTP", {"uri": f"http://127.0.0.1:{server.server_port}/radio"}), reconnect=False)
    try:
        pipeline.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pipeline.snapshot()["stream_title"]:
            time.sleep(0.02)
        assert "Integration Title" in (pipeline.snapshot()["stream_title"] or "")
    finally:
        pipeline.stop()
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_failure_is_real_error(gst, http_files):
    require(gst, "souphttpsrc")
    _, url = http_files
    pipeline = GstPipeline(config("HTTP", {"uri": url + "/does-not-exist"}), timeout_s=2, reconnect=False)
    with pytest.raises(DependencyUnavailable):
        pipeline.start()
    assert pipeline.snapshot()["state"] == "ERROR"
    assert not pipeline._thread.is_alive()


def test_alsa_capture(gst):
    device = os.environ.get("FMHEADEND_TEST_ALSA_DEVICE")
    if not device:
        pytest.skip("No ALSA capture device selected; set FMHEADEND_TEST_ALSA_DEVICE in the Linux VM")
    require(gst, "alsasrc")
    pipeline = GstPipeline(config("ALSA", {"device": device}), timeout_s=5, reconnect=False)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        assert_pcm(pipeline, gst)
    finally:
        pipeline.stop()


@pytest.mark.parametrize("processor", [None, "builtin"])
def test_native_gain(gst, http_files, processor):
    import array
    require(gst, "souphttpsrc", "wavparse", "volume")
    folder, url = http_files
    with wave.open(str(folder / "gain.wav"), "wb") as wav:
        wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x10\x27" * 2 * 48000 * 5)  # constant 10000 for this test only
    profile = file_profile(name="gain", gain_db=-6.020599913)
    source = config("HTTP", {"uri": url + "/gain.wav"}).model_copy(update={
        'input_gain_db': -6.020599913, 'processor_mode': 'builtin' if processor else 'bypass',
        'audio_profile_path': 'default/basic.json' if processor else None})
    pipeline = GstPipeline(source, profile=profile if processor else None, reconnect=False)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        pcm = assert_pcm(pipeline, gst)
        samples = array.array("h", pcm)
        import sys
        if sys.byteorder != "little":
            samples.byteswap()
        expected = 2500 if processor else 5000
        assert all(expected - 1 <= value <= expected + 1 for value in samples)
    finally:
        pipeline.stop()


@pytest.mark.parametrize("codec,extra", [
    ("OPUS", {}), ("MP3", {}),
    ("PCM", {"clock_rate": 44100, "channels": 1}),
    ("AAC", {"clock_rate": 48000, "channels": 2, "codec_data": "1190"}),
])
def test_native_rtp_caps_fields_survive_construction(gst, codec, extra):
    values = {"codec": codec, "payload_type": 96, **extra}
    _, fields = rtp_parameters(values)
    caps = make_rtp_caps(gst, values, fields)
    with caps_structure(caps) as structure:
        assert structure.get_name() == "application/x-rtp"
        for key, expected in {"media": "audio", "payload": 96, **fields}.items():
            assert structure.get_value(key) == expected


@pytest.mark.parametrize('agent', [None, 'Station-native/2'])
def test_http_user_agent_on_wire(gst,tmp_path,agent):
    require(gst,'souphttpsrc','wavparse')
    with wave.open(str(tmp_path/'ua.wav'),'wb') as wav:
        wav.setparams((2,2,48000,0,'NONE','not compressed'))
        wav.writeframes(b'\0'*4*48000*10)
    seen=[]
    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('icy-name','Deterministic Station')
            super().end_headers()
        def do_GET(self):
            seen.append(self.headers.get('User-Agent'))
            super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    source=InputSourceConfig(name='UA',type='HTTP',config={'uri':f'http://127.0.0.1:{server.server_port}/ua.wav'},
        gst_options={} if agent is None else {'source':{'user-agent':agent}})
    pipeline=GstPipeline(source,reconnect=False)
    try:
        pipeline.start()
        assert seen and all(value==(agent or 'fmheadend/0.1.0') for value in seen)
        assert pipeline.probe_result().station_name == 'Deterministic Station'
    finally:
        pipeline.stop();server.shutdown();server.server_close();thread.join()


def test_modular_native_chain_and_float_domain(gst,http_files):
    require(gst,'audiocheblimit','souphttpsrc','wavparse')
    import math, struct
    folder,url=http_files
    with wave.open(str(folder/'basic.wav'),'wb') as wav:
        wav.setparams((1,2,44100,0,'NONE','not compressed'))
        wav.writeframes(b''.join(struct.pack('<h',int(12000*math.sin(i*2*math.pi*1000/44100))) for i in range(44100*4)))
    profile=file_profile(name='Filters', gain_db=-3)
    for stage in ('hpf', 'lpf'): profile.stages[stage].selected = 'CHEBYSHEV'
    source = config('HTTP',{'uri':url+'/basic.wav'}).model_copy(update={
        'processor_mode': 'builtin', 'audio_profile_path': 'default/basic.json'})
    pipeline=GstPipeline(source,profile=profile,reconnect=False)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        assert_pcm(pipeline,gst)
        # Follow actual negotiated downstream pads rather than construction order.
        element=pipeline._convert
        factories=[]
        while element is not None:
            factory=element.get_factory()
            factories.append(factory.get_name())
            if factory.get_name() in ('volume','audiocheblimit'):
                caps=element.get_static_pad('sink').get_current_caps()
                with caps_structure(caps) as structure:
                    assert structure.get_string('format')=='F32LE'
                    assert structure.get_value('rate')==48000
                    assert structure.get_value('channels')==2
            pad=element.get_static_pad('src')
            peer=pad.get_peer() if pad else None
            element=peer.get_parent_element() if peer else None
        assert factories==['audioconvert','audioresample','capsfilter','volume','volume','audiocheblimit',
                           'volume','audiocheblimit','volume',
                           'audioconvert','capsfilter','queue','appsink']
    finally:pipeline.stop()


def test_off_does_not_construct_processors(gst):
    from unittest.mock import patch
    pipeline=GstPipeline(config('UDP',{'port':54321,'format':'S16LE','sample_rate':48000,'channels':2}).model_copy(
        update={'processor_mode': 'builtin', 'audio_profile_path': 'default/basic.json'}),
                         profile=file_profile(name='None',gain_db=-3))
    pipeline.Gst=gst
    make=gst.ElementFactory.make
    with patch.object(gst.ElementFactory,'make',side_effect=lambda name,*args: (_ for _ in ()).throw(AssertionError('DSP instantiated')) if name in ('audiocheblimit','lsp-plug-in-plugins-lv2-limiter-stereo') else make(name,*args)):
        pipeline._build()
    pipeline._pipeline.set_state(gst.State.NULL)


def test_missing_filter_plugin_fails_explicitly(gst):
    from unittest.mock import patch
    pipeline=GstPipeline(config('UDP',{'port':54321,'format':'S16LE','sample_rate':48000,'channels':2}).model_copy(
        update={'processor_mode': 'builtin', 'audio_profile_path': 'default/basic.json'}),
                         profile=file_profile(name='Filters'))
    pipeline.profile.stages['hpf'].selected = 'CHEBYSHEV'
    pipeline.Gst=gst
    make=gst.ElementFactory.make
    with patch.object(gst.ElementFactory,'make',side_effect=lambda name,*args:None if name=='audiocheblimit' else make(name,*args)):
        with pytest.raises(DependencyUnavailable,match='Missing GStreamer element: audiocheblimit'):
            pipeline._build()
    pipeline._pipeline.set_state(gst.State.NULL)


@pytest.mark.parametrize('weighting,tau', [('50us', 50e-6), ('75us', 75e-6)])
def test_native_weighting_inverse_and_limiter_wrapper(gst, weighting, tau):
    """Short isolated F32 stereo render: real property arrays, negotiation and PCM."""
    import array
    import math
    from fmheadendd.sources.dsp import dsp_stages, fm_limiter_coefficients, PROCESSING_CAPS
    limiter = 'lsp-plug-in-plugins-lv2-limiter-stereo'
    require(gst, 'appsrc', 'appsink', 'audioiirfilter', limiter)
    profile = file_profile('Weighting')
    profile.stages['limiter'].selected = 'LSP_LIMITER'
    profile.stages['limiter'].implementations['LSP_LIMITER']['fm_limiter_weighting'] = weighting
    GstPipeline.validate_profile(profile)  # includes native readback checks
    samples = array.array('f', [v for i in range(4096) for v in
        (.01 * math.sin(2*math.pi*1000*i/48000), .02 * math.sin(2*math.pi*3000*i/48000))])
    for wrapped in (False, True):
        pipeline = gst.Pipeline.new(None)
        def make(factory, **properties):
            element = gst.ElementFactory.make(factory, None)
            for key, value in properties.items(): element.set_property(key.replace('_', '-'), value)
            pipeline.add(element)
            return element
        src = make('appsrc', caps=gst.Caps.from_string(PROCESSING_CAPS), format=gst.Format.TIME)
        if wrapped:
            elements = dsp_stages(make, profile)
        else:
            elements = [make('audioiirfilter', **p) for p in fm_limiter_coefficients(48000, tau)]
        sink = make('appsink', sync=False, wait_on_eos=False)
        chain = [src, *elements, sink]
        for left, right in zip(chain, chain[1:]): assert left.link(right)
        try:
            assert pipeline.set_state(gst.State.PLAYING) != gst.StateChangeReturn.FAILURE
            data = samples.tobytes()
            buf = gst.Buffer.new_allocate(None, len(data), None)
            buf.fill(0, data)
            buf.pts = 0
            buf.duration = 4096 * gst.SECOND // 48000
            assert src.emit('push-buffer', buf) == gst.FlowReturn.OK
            src.emit('end-of-stream')
            result = sink.emit('try-pull-sample', 3 * gst.SECOND)
            assert result is not None
            assert result.get_caps().is_equal(gst.Caps.from_string(PROCESSING_CAPS))
            rendered = array.array('f')
            buffer = result.get_buffer()
            rendered.frombytes(buffer.extract_dup(0, buffer.get_size()))
            assert len(rendered) == len(samples)
            assert all(math.isfinite(x) for x in rendered) and max(map(abs, rendered)) > .005
            if not wrapped:
                assert list(rendered) == pytest.approx(list(samples), abs=1e-7)
        finally:
            pipeline.set_state(gst.State.NULL)


@pytest.mark.parametrize('kind', ['HTTP', 'UDP'])
def test_native_start_without_sender_remains_owned_until_stop(gst, http_files, kind):
    require(gst, 'souphttpsrc' if kind == 'HTTP' else 'udpsrc')
    _, url = http_files
    values = {'uri': url + '/absent'} if kind == 'HTTP' else {
        'host': '127.0.0.1', 'port': free_udp_port(), 'format': 'S16LE', 'sample_rate': 48000, 'channels': 2}
    pipeline = GstPipeline(config(kind, values), timeout_s=.1)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        assert pipeline._prepared.is_set()
        assert not pipeline._ready.is_set()
        assert pipeline._thread.is_alive()
        assert not pipeline.probe_result().ok
        assert pipeline.output.read(0) is None
    finally:
        pipeline.stop()
    assert not pipeline._thread.is_alive()
