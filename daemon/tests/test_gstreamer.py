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
from fmheadendd.models.configuration import InputSourceConfig, AudioProfileConfig
from fmheadendd.sources.gst_pipeline import GstPipeline, load_gst, caps_structure, make_rtp_caps, rtp_parameters

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


def test_native_gain(gst, http_files):
    import array
    require(gst, "souphttpsrc", "wavparse", "volume")
    folder, url = http_files
    with wave.open(str(folder / "gain.wav"), "wb") as wav:
        wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x10\x27" * 2 * 48000 * 5)  # constant 10000 for this test only
    profile = AudioProfileConfig(name="gain", processing_enabled=True, gain_db=-6.020599913)
    pipeline = GstPipeline(config("HTTP", {"uri": url + "/gain.wav"}), profile=profile, reconnect=False)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        pcm = assert_pcm(pipeline, gst)
        samples = array.array("h", pcm)
        import sys
        if sys.byteorder != "little":
            samples.byteswap()
        assert all(4999 <= value <= 5001 for value in samples)
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
