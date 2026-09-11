"""Native daemon integration tests, run by CTest with only Python's stdlib."""
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = sys.argv.pop(1)


def status(directory, request=b'{"command":"get_status"}\n'):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(directory / "mux-control.sock"))
        client.sendall(request)
        data = b""
        while b"\n" not in data:
            block = client.recv(4096)
            if not block:
                raise AssertionError("Incomplete control response")
            data += block
        return json.loads(data)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fmux-")
        self.directory = Path(self.temp.name)
        self.capture = self.directory / "frames.bin"
        self.log = (self.directory / "daemon.log").open("w+")
        self.process = subprocess.Popen([BINARY, "--transport", "mock", "--runtime-dir", str(self.directory),
                                         "--capture", str(self.capture), "--capture-frames", "1000"], stdout=self.log, stderr=self.log)
        self.clients = []
        deadline = time.monotonic() + 5
        while True:
            try:
                if status(self.directory)["frames_sent"] > 0:
                    break
            except (OSError, AssertionError):
                pass
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.log.seek(0)
                self.fail(self.log.read())
            time.sleep(0.01)

    def tearDown(self):
        for client in self.clients:
            client.close()
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        self.log.close()
        self.temp.cleanup()

    def connect(self, channel):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(self.directory / f"pcm{channel}.sock"))
        self.clients.append(client)
        return client

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = status(self.directory)
            if predicate(result):
                return result
            time.sleep(0.01)
        self.fail("Timed out waiting for daemon status")

    def test_all_eight_sockets_are_independent(self):
        for channel in range(8):
            client = self.connect(channel)
            client.sendall(bytes([channel + 1]) * 2048)
            result = self.wait_for(lambda s: s["channels"][channel]["bytes_consumed"] > 0)
            self.assertEqual(result["channels"][channel]["bytes_received"], 2048)
            for untouched in range(channel + 1, 8):
                self.assertEqual(result["channels"][untouched]["bytes_received"], 0)
        self.assertEqual(len(list(self.directory.glob("pcm*.sock"))), 8)

    def test_control_and_silence_counter_frames(self):
        initial = status(self.directory)
        result = self.wait_for(lambda s: s["frames_sent"] > initial["frames_sent"] + 10)
        self.assertTrue(result["mocked"])
        self.assertIsNone(result["spi_ok"])
        self.assertEqual(result["transport_state"], "RUNNING")
        self.assertTrue(all(c["underruns"] > 0 for c in result["channels"]))
        self.process.terminate(); self.process.wait(timeout=5)
        raw = self.capture.read_bytes()
        self.assertGreater(len(raw), 4100)
        self.assertEqual(len(raw) % 4100, 0)
        for index in range(len(raw) // 4100):
            frame = raw[index * 4100:(index + 1) * 4100]
            self.assertEqual(struct.unpack_from("<I", frame)[0], index)
            self.assertEqual(frame[4:], bytes(4096))

    def test_reconnect_and_fragmented_stereo(self):
        client = self.connect(3)
        client.sendall(b"\x99\x88\x77")
        self.wait_for(lambda s: s["channels"][3]["bytes_received"] == 3)
        client.close()
        self.wait_for(lambda s: not s["channels"][3]["connected"])
        client = self.connect(3)
        payload = bytes([0x34, 0x12, 0xcc, 0xed]) * 128
        client.sendall(payload[:1]); client.sendall(payload[1:3]); client.sendall(payload[3:])
        self.wait_for(lambda s: s["channels"][3]["bytes_consumed"] >= 512)
        self.process.terminate(); self.process.wait(timeout=5)
        frames = self.capture.read_bytes()
        # The complete stereo sequence may span adjacent frames; other channels stay silent.
        data = b"".join(frames[i + 4 + 3 * 512:i + 4 + 4 * 512] for i in range(0, len(frames), 4100))
        self.assertIn(payload[:4], data)
        for i in range(0, len(frames), 4100):
            self.assertEqual(frames[i + 4:i + 4 + 3 * 512], bytes(3 * 512))
            self.assertEqual(frames[i + 4 + 4 * 512:i + 4100], bytes(4 * 512))

    def test_invalid_control_and_partial_request(self):
        self.assertEqual(status(self.directory, b'{"command":"unknown"}\n')["error"], "INVALID_COMMAND")
        self.assertEqual(status(self.directory, b'{"command":"get_ status"}\n')["error"], "INVALID_COMMAND")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.directory / "mux-control.sock"))
            client.sendall(b'{"command":')
            client.sendall(b'"get_status"}\n')
            data = b""
            while b"\n" not in data:
                data += client.recv(4096)
            self.assertEqual(json.loads(data)["state"], "RUNNING")

    def test_second_daemon_does_not_unlink_live_sockets(self):
        other = subprocess.run([BINARY, "--transport", "mock", "--runtime-dir", str(self.directory)], capture_output=True, timeout=5)
        self.assertNotEqual(other.returncode, 0)
        self.assertEqual(status(self.directory)["state"], "RUNNING")

    def test_missing_spidev_is_not_mocked(self):
        other = subprocess.run([BINARY, "--transport", "spidev", "--spi-device", str(self.directory / "missing")], capture_output=True, timeout=5)
        self.assertNotEqual(other.returncode, 0)


if __name__ == "__main__":
    unittest.main()
