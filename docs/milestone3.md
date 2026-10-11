# Milestone 3: Linux PCM mux

With Milestone 4 installed, add `--mock-rp` to the Python daemon commands below
to retain the hardware-free RP control used in this milestone. See [Milestone 4](milestone4.md).

Milestone 3 adds an independent C++17 `fmheadend-muxd` and Python Unix mux client.
No architectural deviation is introduced. RP2354 I2C/QN control remains mocked;
RDS encoding/scheduling, firmware, systemd deployment and the Web UI remain later
milestones. Neither daemon starts the other.

## Build and test on Raspberry Pi OS

Run from the repository root. Keep build products and the Linux virtual environment
on local Linux storage when the repository is on an SMB share.

```sh
sudo apt update
sudo apt install build-essential cmake
cmake -S muxd -B "$HOME/build/fmheadend-muxd-m3" -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE="$HOME/venvs/fmheadend/bin/python"
cmake --build "$HOME/build/fmheadend-muxd-m3" -j2
ctest --test-dir "$HOME/build/fmheadend-muxd-m3" --output-on-failure
MUXD_BINARY="$HOME/build/fmheadend-muxd-m3/fmheadend-muxd" \
  FMHEADEND_TEST_ALSA_DEVICE=plughw:2,0 \
  "$HOME/venvs/fmheadend/bin/python" -m pytest daemon/tests -q -ra
```

The existing venv must contain the editable `daemon[test]` package and expose
native GI bindings. For a new machine, see [Milestone 2 dependencies](milestone2.md).
If needed, create it with `python3 -m venv --system-site-packages "$HOME/venvs/fmheadend"`
and install with `"$HOME/venvs/fmheadend/bin/python" -m pip install -e './daemon[test]'`.
Omit `FMHEADEND_TEST_ALSA_DEVICE` if no capture device is available; that test then
skips explicitly. Native mux integration skips if `MUXD_BINARY` is unavailable.

No Python dependency was added in Milestone 3. On the validation Pi, GCC was
already installed. Installing CMake added: `cmake`, `cmake-data`, `libarchive13t64`,
`libjsoncpp26`, `librhash1`, `libuv1t64`.

Validation on Raspberry Pi OS / Debian 13, Python 3.13.5, GCC 14.2:

| Suite | Passed | Failed | Skipped |
|---|---:|---:|---:|
| CTest (7 C++ core cases plus socket integration) | 8 | 0 | 0 |
| Python regression, native GStreamer and Behringer ALSA | 98 | 0 | 0 |
| Windows Python regression | 79 | 0 | 19 |

The CTest socket entry contains six Python stdlib tests against the native daemon.
Windows skips 15 GStreamer tests and four Linux mux integration tests. Python
reports two existing Starlette/AnyIO deprecation warnings. No RP2354 or physical
SPI transfer was tested. The spidev implementation was compiled and its missing
device failure path tested. HTTP-to-GStreamer-to-Unix-to-C++ PCM is tested with a
local HTTP WAV server; ALSA ingest uses the actual `plughw:2,0` USB capture device.

## Run independently

Create the runtime directory after each reboot (systemd units are a later milestone):

```sh
sudo install -d -m 0770 -o "$(id -un)" -g "$(id -gn)" /run/fmheadend
"$HOME/build/fmheadend-muxd-m3/fmheadend-muxd" --transport mock
```

In a second terminal, from the repository root:

```sh
"$HOME/venvs/fmheadend/bin/fmheadendd" --database "$HOME/fmheadend-m3-smoke.db" \
  --host 127.0.0.1 --port 8080
```

Mock transport discards complete frames, reports `transport=mock`, `mocked=true`
and `spi_ok=null`; it never claims hardware availability. Optional mock capture:
`--capture /tmp/fmheadend-frames.bin --capture-frames 3750` records at most ten
seconds, then closes the file while pumping continues. The default capture limit
is 375 frames; the maximum is 100000. Capture files contain consecutive complete
4100-byte frames and are overwritten on startup. Disk capture is diagnostic and
may perturb timing; normal mock operation does no file writes.

Both daemons accept `--mux-runtime-dir` (Python) / `--runtime-dir` (C++) overrides
for tests. Defaults expose precisely `/run/fmheadend/pcm0.sock` through `pcm7.sock`
and `/run/fmheadend/mux-control.sock`. A directory lock prevents competing mux
instances from replacing live sockets. PCM and control use separate SOCK_STREAM
connections. One producer is accepted per channel; a simultaneous second producer
is closed. Socket access is restricted to the owning user/group by the runtime
umask. No authentication infrastructure is added.

## Exact SPI binary layout

All offsets are bytes. PCM is fixed 48000 Hz, signed 16-bit little-endian, stereo,
interleaved L/R. There are no PCM socket packet headers. Socket number selects the
channel; arbitrary stream fragmentation is reassembled at four-byte stereo boundaries.

| Offset | Length | Contents |
|---:|---:|---|
| 0 | 4 | Unsigned 32-bit little-endian frame counter |
| 4 | 512 | Channel 0: 128 stereo samples |
| 516 | 512 | Channel 1 |
| 1028 | 512 | Channel 2 |
| 1540 | 512 | Channel 3 |
| 2052 | 512 | Channel 4 |
| 2564 | 512 | Channel 5 |
| 3076 | 512 | Channel 6 |
| 3588 | 512 | Channel 7 |

Total: **4100 bytes**, comprising a four-byte header and 4096-byte PCM payload.
For channel `c` and stereo sample `s`, left is at `4 + 512*c + 4*s`, right two
bytes later. Each sample stores its least significant byte first. Channel blocks
are consecutive; channels are not interleaved sample by sample.

Counter starts at zero on daemon startup and wraps modulo 2^32. It increments
for every built frame, including failed transport attempts, allowing downstream
gap detection. There is no magic, version, length, checksum or additional padding
in this initial format. The SPI chip-select transaction supplies the frame boundary.
A single `SPI_IOC_MESSAGE(1)` transfers the entire 4100-byte frame under one CS.

Nominal cadence: 375 frames/second, 128/48000 seconds (about 2.667 ms) per frame.
A steady clock with fractional nanosecond carry paces output. Late execution
increments `late_frames` and does not queue a catch-up burst. Linux scheduling is
not a hard real-time guarantee; Zero 2 performance/stress validation remains in
Milestone 6.

## Buffers and counters

Each channel ring defaults to 57600 bytes (300 ms), configured per process with
`--ring-buffer-ms` and stored per ring instance. A short 512-byte read zero-fills
missing audio and increments `underruns` once per deficient channel/frame,
including entirely disconnected channels. Each overflowing ring append discards
the oldest complete stereo samples and increments `overruns` once. These counters
count events, not samples. Disconnect clears buffered and partial PCM, so a new
producer cannot inherit stale audio or a partial stereo sample.

`bytes_received` counts socket bytes, including partial samples;
`bytes_consumed` counts actual PCM placed in frames, excluding generated silence.
Counters persist across producer reconnects and reset when the daemon restarts.
All eight slots are emitted continuously, including when every producer is absent.

Python keeps a bounded queue per consumer (default 300 ms capacity and age,
`--pcm-consumer-buffer-ms`). One independent
writer thread drains those queues, with at most one pending bounded chunk per
channel. Writes are nonblocking; a partial write stalled for 100 ms closes the
session before discarding it to preserve sample alignment. Failed connections
retry after 250 ms without replaying a partially written chunk. Kernel socket
buffers are also bounded and contribute additional latency; 300 ms describes
the ring target, not an end-to-end latency guarantee. Appsink performs no socket
or hardware I/O. Shared sources still use one pipeline, with separate consumer
queues. ANALOG modulators create neither pipeline nor PCM producer.

## Control and API diagnostics

Send `{"command":"get_status"}\n` on a fresh control connection. Receive one JSON
object followed by newline, then EOF. Requests are limited to 1024 bytes, peers
to 16 concurrent connections, and each connection to two seconds. Invalid
commands return `INVALID_COMMAND`. No control data appears on PCM sockets.

Response fields: `state`, `transport`, `mocked`, `transport_state`, `spi_ok`,
`frames_sent`, `frames_built`, `transport_errors`, `spi_errors`, `late_frames`,
`last_error`, and eight `channels` entries containing `id`, `connected`,
`buffered_bytes`, `underruns`, `overruns`, `bytes_received`, `bytes_consumed`.
`state=RUNNING` describes the daemon; transport can separately be `STARTING`,
`RUNNING` or `ERROR`. `frames_sent` counts successful transport calls.

The existing cached `/api/v1/status` exposes `system.mux_transport`,
`mux_transport_state`, `mux_frames_sent`, `mux_mocked`, plus each modulator's
`underruns`, `overruns`, `pcm_bytes_received`, `pcm_bytes_consumed`. Updates follow
the existing status polling interval. Routes do not access sockets. The existing
broad `mocked` fields still reflect mocked RP2354 control; use `mux_mocked` for
the selected audio transport.

## Manual real-source smoke test

Start both daemons as above. Create an ALSA source using the connected Behringer:

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Behringer USB","type":"ALSA","config":{"device":"plughw:2,0","channel_selection":"stereo"}}'
```

Alternatively create an HTTP/HLS source, replacing the URI with a working stream:

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"HTTP smoke","type":"HTTP","config":{"uri":"https://example.org/live.m3u8"}}'
```

Use the returned ID below instead of `1` if necessary. Frequency is explicitly
configured; no default transmitter frequency is introduced. RF control is mocked.

```sh
curl -sS -X PATCH http://127.0.0.1:8080/api/v1/modulators/0 \
  -H 'Content-Type: application/json' \
  -d '{"input_source_id":1,"frequency_khz":98100}'
curl -sS -X POST http://127.0.0.1:8080/api/v1/modulators/0/start
curl -sS http://127.0.0.1:8080/api/v1/sources/1/status
curl -sS http://127.0.0.1:8080/api/v1/status
sleep 6
curl -sS http://127.0.0.1:8080/api/v1/status
```

Confirm source `RUNNING` and output caps `audio/x-raw`, `format=S16LE`,
`rate=48000`, `channels=2`, `layout=interleaved`. Confirm modulator 0's
`pcm_bytes_received` **and** `pcm_bytes_consumed` increase between snapshots.
These are reported by the C++ daemon, proving PCM arrived and entered mux frames;
GStreamer RUNNING alone is insufficient. `mux_frames_sent` also increases and
`mux_transport=mock` explicitly identifies the unconnected hardware boundary.
Silence at the USB analog input still produces valid captured PCM.

```sh
curl -sS -X POST http://127.0.0.1:8080/api/v1/modulators/0/stop
```

After status refresh, PCM byte counters stop increasing while frame and underrun
counters continue. This demonstrates that missing producers become digital silence.

## Real spidev transport (hardware bring-up only)

Enable the Pi SPI interface and grant the daemon user device access. The full
4100-byte frame exceeds a typical 4096-byte spidev buffer. Configure
`spidev.bufsiz=8192` before loading the driver; on Raspberry Pi OS this can be
appended to the existing single line in `/boot/firmware/cmdline.txt`, followed by
reboot. Verify `cat /sys/module/spidev/parameters/bufsiz` is at least 4100.
The daemon refuses a known insufficient buffer rather than splitting CS frames.
No boot configuration was changed during this milestone.

```sh
"$HOME/build/fmheadend-muxd-m3/fmheadend-muxd" --transport spidev \
  --spi-device /dev/spidev0.0 --spi-clock-hz 25000000 --spi-mode 0
```

Initial target is 25 MHz, 8 bits per word, MSB first on the wire, SPI mode 0;
clock and mode are configurable for bring-up. Byte order within multibyte values
remains little-endian. The controller may select a supported actual clock.
Open/configuration failures terminate startup with a useful error; transfer
failures increment counters and expose ERROR without falling back to mock.
A successful SPI ioctl indicates host transfer success, **not** RP2354 presence
or reception: SPI has no peripheral acknowledgement. See the
[Linux spidev documentation](https://docs.kernel.org/spi/spidev.html).
