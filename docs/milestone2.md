# Milestone 2: GStreamer source ingest

With Milestones 3-4 installed, use `--mock-rp --mock-mux` on the Python daemon
for standalone Milestone 2 operation. See [Milestone 4 modes](milestone4.md).

## Dependencies and installation

New runtime dependencies are the native GStreamer 1.x libraries/plugins and
PyGObject (`gi`), including the distro GStreamer Python overrides. No new pip
library, ORM, broker, process supervisor or external media process is introduced.
The Python web/test dependencies are unchanged. No Linux packages were installed
by the implementation session: its accessible execution host was Windows, not
the Linux Mint VM.

For Linux Mint 22.x (system Python 3.12), from the repository root:

```sh
sudo apt update
sudo apt install python3-venv python3-gi python3-gst-1.0 \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-alsa alsa-utils
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -e './daemon[test]'
python -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
fmheadendd --database ./fmheadend.db --host 127.0.0.1 --port 8080
```

`alsa-utils` provides `arecord` for device discovery. The bad/ugly/libav plugin
sets provide additional codecs and HLS/test fixture tools. See the upstream
[GStreamer Linux installation guide](https://gstreamer.freedesktop.org/documentation/installing/on-linux.html)
and [PyGObject setup instructions](https://pygobject.gnome.org/getting_started.html).

Do not reuse a venv created without system-site-packages if it cannot import `gi`.
The existing project requires Python >=3.11; Mint 21's default Python 3.10 is not
sufficient. No Python requirement has been lowered or silently bypassed.

## Transport and processing behavior

- HTTP/HLS: `uridecodebin` handles URI opening, demux and decode. HTTP sources
  request ICY metadata through `souphttpsrc`. TAG messages expose title, artist,
  codec and bitrate where supplied. Source runtime metadata feeds the existing
  RadioText selection path; there is no RDS encoding or scheduling.
- UDP: explicit raw PCM format, input sample rate and channel count are required.
  Host is the receive address; multicast_group, when set, selects the multicast
  group to join. The PCM producer must send whole input frames in each datagram.
- RTP: a bounded jitterbuffer feeds the selected depayloader and decoder.
  OPUS uses OPUS/48000; MP3 uses MPA/90000. PCM means network-order L16 and requires
  explicit `clock_rate` and `channels`. AAC means MPEG4-GENERIC AAC-hbr and requires
  `clock_rate`, `channels` and hexadecimal `codec_data` (AudioSpecificConfig).
  These optional config fields are additive; existing M1 source records still
  load. Missing required RTP transport details produce a useful probe/start
  error rather than guessed format information. Other RTP packetizations are
  outside this implementation.
- ALSA: `alsasrc` captures the selected device at configured input rate/channels.
  Capture-device negotiation failure is reported, not converted to mock success.
- ANALOG: source acquisition and both source probes bypass the pipeline factory,
  even if GI is missing. No PCM queue/producer is created for an analog TX.
- Every digital path uses `audioconvert`, `audioresample` and native `volume` and
  terminates in caps `audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved`.
  The callback checks actual sample caps before accepting PCM. No sample arithmetic
  occurs in production Python code.
- AudioProfile `processing_enabled=false` bypasses gain. Enabled profiles apply
  `gain_db` with GStreamer's volume element (maximum +20 dB). Configured EQ,
  compressor, limiter or advanced processing is rejected explicitly when enabled;
  these model hooks remain available but are not silently represented as applied.
  Profile edits are applied on the source's next start.
- `gst_options` supports controlled `source` (`timeout`, `user-agent`,
  `buffer-size`), `queue` (`max-size-time`) and `resample` (`quality`,
  `sinc-filter-mode`) properties where the selected element supports them.
  Unsupported options fail clearly. HTTP timeout is in seconds and must fit the
  probe deadline; UDP's timeout property is in nanoseconds. Queue byte/buffer
  limits cannot be disabled through overrides. Arbitrary pipeline strings are
  not accepted by the API.

The [appsink documentation](https://gstreamer.freedesktop.org/documentation/app/appsink.html)
describes the streaming-thread callback and internal buffering. Here the callback
only checks caps and copies at most 19,200 bytes into a non-waiting bounded queue.
A worker handles bus messages and fans blocks out to consumer queues. The native
upstream queue applies bounded backpressure to decoding, so HLS segments/files
are paced rather than discarded simply because decoding runs faster than audio.
Each output queue holds at most 100 ms, drops oldest data on overflow, and refuses
to return data older than 100 ms. An empty read returns None for the future
transport to interpret as silence. There is no SPI frame construction or mux IPC.

## Lifecycle, reconnect and probes

One active digital InputSource has one native pipeline. The first consumer starts
it; subsequent consumers reuse it; the last consumer stops it. A transmitter is
not enabled until the first valid PCM block arrives. Initial start failure leaves
no active consumer and returns 503 with desired config retained, as in M1.

Once running, ERROR, EOS and audio inactivity clear pending PCM and reconnect
with 1, 2, 5, 10, 30, 30... second backoff. Status reports CONNECTING with the last
error during retries. Reconnect count increments when a retry begins. Thirty
seconds of stable playback resets backoff. Stream failure does not toggle RF.
Stopping interrupts retry waits and joins the source worker. Recovery of the RP
or mux hardware is not implemented.

A probe waits up to the HTTP connect_timeout_ms (clamped to 1..30 seconds), or
10 seconds for other types, for the first valid PCM. It reports input format
information and output_caps separately. Teardown can take additional time up to
the native IO timeout. Probes have no persistent side effects and do not change
assignments. An active saved-source probe reads the existing pipeline snapshot;
an inactive/unsaved probe opens a temporary pipeline and always stops it.
ALSA unsaved probes can fail if the device is already exclusively in use.

All control/SQLite calls execute serially on one dedicated executor thread, while
ASGI and source workers run independently. At most 32 control calls may be pending;
overload returns 503 CONTROL_BUSY. A slow probe can delay subsequent control
requests, but cannot block media callbacks or the HTTP event loop. Cached hardware
status, desired-state persistence, nullable unconfigured frequency, explicit
start/stop, RT overrides and `--mock` behavior are retained.

## Manual smoke tests

With the daemon running in real mode, replace the URI with a reachable HTTP radio
stream or HLS playlist. No transmitter assignment or frequency is needed to probe:

```sh
curl -sS http://127.0.0.1:8080/api/v1/sources/test \
  -H 'Content-Type: application/json' \
  -d '{"name":"HTTP or HLS test","type":"HTTP","config":{"uri":"https://YOUR-SERVER/live.m3u8"}}'
```

For ALSA, first list capture devices using `arecord -l`, then use the appropriate
device (VirtualBox audio input must be enabled, or pass a USB capture device to
the VM):

```sh
curl -sS http://127.0.0.1:8080/api/v1/sources/test \
  -H 'Content-Type: application/json' \
  -d '{"name":"ALSA test","type":"ALSA","config":{"device":"plughw:1,0","sample_rate":48000,"channels":2}}'
```

Expect `ok: true`, `mocked: false` and output_caps containing S16LE/48000/2 on
success. `sample_rate` and `channels` describe the detected input, not the fixed
output. A connection/device failure must return `ok: false` with a message.
For the saved-source path, POST the same body to `/api/v1/sources`, then POST
`/api/v1/sources/{returned_id}/test`.

## Automated verification

```sh
python -m pytest daemon/tests -q -ra
python -m pytest daemon/tests -m gstreamer -v -ra
FMHEADEND_TEST_ALSA_DEVICE=plughw:1,0 python -m pytest daemon/tests -m gstreamer -v -ra
```

Native tests create their own local WAV/HTTP, HLS and ICY fixtures and UDP/RTP
senders. They do not depend on public internet radio or FM hardware. They assert
negotiated appsink caps and actual PCM delivery. A native gain test inspects
samples only in test code. Missing GI/plugin dependencies cause explicit skips;
when dependencies are present, connection/decoding failures fail the tests.
ALSA is opt-in because a VM may have no usable capture device.

Implementation-host verification: 60 passed, 0 failed, 11 skipped. All 47 M1
regressions passed. The 11 native tests skipped because the accessible Windows
Python environment lacks GI/GStreamer; native caps negotiation, codec playback,
HLS/ICY and ALSA have **not** been verified on Linux Mint in this session. Two
existing upstream test-dependency deprecation warnings remain. No successful
integration run or Pi performance result is claimed.

No architectural deviation. No later-milestone components were implemented.

## Debian 13 / gst-python 1.26 structure compatibility

Caps reads now enter the public structure context manager when the binding
returns one, with a plain-structure fallback for other versions. All borrowed
structure reads remain within that scope. RTP caps are populated through an
owned `Gst.Structure.new_empty()` and then appended to caps, so no borrowed
structure is mutated. Native caps assertions use the same scoped access.
No private wrapper internals, dependency pinning, architecture changes or UDP
timeout changes are involved.

Non-native compatibility regression run: **69 passed, 0 failed, 15 native tests
deselected**, with two existing dependency warnings. The additional tests model
context-only wrappers and enforce borrowed-structure lifetimes. Native Debian
verification must be rerun on the Raspberry Pi; this local run does not establish
that the earlier native failures are resolved.

From the repository root on the Raspberry Pi, using the existing GI-enabled venv:

```sh
. .venv/bin/activate
python -m pytest daemon/tests/test_gstreamer.py -v -ra
python -m pytest daemon/tests -q -ra
```

ALSA remains an expected skip unless `FMHEADEND_TEST_ALSA_DEVICE` is set. Only if
the raw UDP test still fails after the structure fix, collect its separate log:

```sh
GST_DEBUG=2 python -m pytest 'daemon/tests/test_gstreamer.py::test_loopback_udp_rtp[UDP]' -vv -s
```
