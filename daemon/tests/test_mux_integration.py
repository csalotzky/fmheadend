"""End-to-end Linux tests against the built C++ daemon, using mock SPI only."""
from pathlib import Path
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.sources.gst_pipeline import GstPipeline, load_gst
from fmheadendd.sources.output import PcmOutput

pytestmark = pytest.mark.muxd


@pytest.fixture
def mux_process():
    if not sys.platform.startswith("linux"):
        pytest.skip("fmheadend-muxd integration requires Linux")
    binary = Path(os.environ.get("MUXD_BINARY", Path(__file__).resolve().parents[2] / "muxd/build/fmheadend-muxd"))
    if not binary.is_file():
        pytest.skip("Build muxd first or set MUXD_BINARY to the C++ daemon")
    with tempfile.TemporaryDirectory(prefix="fmux-py-") as directory:
        root = Path(directory)
        with (root / "mux.log").open("w+") as log:
            process = subprocess.Popen([str(binary), "--transport", "mock", "--runtime-dir", directory], stdout=log, stderr=log)
            client = UnixMuxClient(lambda unused: None, root)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        if client.get_status()["frames_sent"] > 0:
                            break
                    except DependencyUnavailable:
                        pass
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.01)
                yield root, process
            finally:
                client.close()
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


def wait_for(client, condition):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get_status()
        if condition(result):
            return result
        time.sleep(0.01)
    pytest.fail("Mux status did not reach the expected state")


def test_python_output_reaches_all_eight_cpp_channels(mux_process):
    root, process = mux_process
    output = PcmOutput()
    client = UnixMuxClient(lambda unused: output, root)
    try:
        for channel in range(8):
            output.attach(channel)
            client.attach(channel, 1)
        payload = b"\x34\x12\xcc\xed" * 128
        output.publish(payload)
        result = wait_for(client, lambda s: all(c["bytes_consumed"] >= 512 for c in s["channels"]))
        assert result["mocked"] and result["spi_ok"] is None
        assert all(c["bytes_received"] == 512 for c in result["channels"])
        for channel in range(8):
            client.detach(channel)
        wait_for(client, lambda s: all(not c["connected"] for c in s["channels"]))
    finally:
        client.close()


def test_python_producer_detach_and_reconnect(mux_process):
    root, process = mux_process
    output = PcmOutput(); output.attach(2)
    client = UnixMuxClient(lambda unused: output, root)
    try:
        for expected in (512, 1024):
            client.attach(2, 1)
            output.publish(b"\x01\x00\xff\xff" * 128)
            wait_for(client, lambda s: s["channels"][2]["bytes_consumed"] >= expected)
            client.detach(2)
            wait_for(client, lambda s: not s["channels"][2]["connected"])
    finally:
        client.close()


def test_mux_disappearance_is_reported_without_mock_fallback(mux_process):
    root, process = mux_process
    client = UnixMuxClient(lambda unused: None, root)
    try:
        assert client.get_status()["state"] == "RUNNING"
        process.terminate(); process.wait(timeout=5)
        with pytest.raises(DependencyUnavailable):
            client.get_status()
    finally:
        client.close()


@pytest.mark.gstreamer
@pytest.mark.parametrize("restart", [False, True])
def test_real_http_gstreamer_to_mux_via_existing_api(mux_process, restart):
    try:
        Gst = load_gst()
    except DependencyUnavailable as exc:
        pytest.skip(str(exc))
    for plugin in ("souphttpsrc", "wavparse"):
        if Gst.ElementFactory.find(plugin) is None:
            pytest.skip("Missing native plugin: " + plugin)
    root, process = mux_process
    with wave.open(str(root / "input.wav"), "wb") as wav:
        wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x10\x27" * 44100 * 10)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    app = create_app(Settings(root / "config.db"), pipeline_factory=GstPipeline,
                     mux_factory=lambda sources: UnixMuxClient(sources.output_for, root))
    observer = UnixMuxClient(lambda unused: None, root)
    replacement = None
    try:
        with TestClient(app) as api:
            response = api.post("/api/v1/sources", json={"name": "native", "type": "HTTP", "config": {"uri": f"http://127.0.0.1:{server.server_port}/input.wav"}})
            assert response.status_code == 201
            source_id = response.json()["id"]
            for channel in (0, 7):
                assert api.patch(f"/api/v1/modulators/{channel}", json={"input_source_id": source_id, "frequency_khz": 98100, "rds_enabled": True, "rds_ecc": 224}).status_code == 200
                started = api.post(f"/api/v1/modulators/{channel}/start")
                assert started.status_code == 200, started.text
            wait_for(observer, lambda s: s["channels"][0]["bytes_consumed"] > 0 and s["channels"][7]["bytes_consumed"] > 0)
            assert len(app.state.services.sources.pipelines) == 1
            if restart:
                services=app.state.services
                deadline=time.monotonic()+5
                while not services.rp.rds_history and time.monotonic()<deadline:
                    time.sleep(0.05)
                assert services.rp.rds_history
                pipeline=services.sources.pipelines[source_id]
                process.kill();process.wait(timeout=5)
                app.state.control.executor.submit(services.status.refresh).result()
                assert api.get('/api/v1/status').json()['system']['system_state']=='DEGRADED'
                assert api.get('/api/v1/modulators/0').json()['enabled']
                replacement=subprocess.Popen(process.args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:
                        current=observer.get_status()
                        if all(current['channels'][i]['bytes_consumed']>0 for i in (0,7)):
                            break
                    except DependencyUnavailable:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail('PCM did not resume after real mux process restart')
                app.state.control.executor.submit(services.status.refresh).result()
                assert api.get('/api/v1/status').json()['system']['system_state']=='RUNNING'
                assert services.sources.pipelines[source_id] is pipeline
                assert services.rp.enabled[0] and services.rp.enabled[7]
            for channel in (0, 7):
                assert api.post(f"/api/v1/modulators/{channel}/stop").status_code == 200
            wait_for(observer, lambda s: not s["channels"][0]["connected"] and not s["channels"][7]["connected"])
    finally:
        observer.close()
        if replacement is not None:
            replacement.terminate();replacement.wait(timeout=5)
        server.shutdown(); server.server_close(); thread.join()
