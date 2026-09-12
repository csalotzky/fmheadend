# Milestone 4: real Linux RP2354 control

For Linux RDS encoding, nullable ECC/AF configuration, scheduling and refill added
after M4, see [Milestone 5](milestone5.md). The wire contract remains in the existing protocol document.

Milestone 4 implements the real client in `daemon/rp2354/linux_client.py` and the
current binary protocol in `daemon/rp2354/protocol.py`. The existing high-level
`MockRp2354Client` is unchanged. There is no fake I2C subsystem: unit tests patch
only low-level OS calls. No new Python dependency or system package was installed.

[Exact payload layouts for all eleven commands](rp2354-protocol.md) are the firmware
handoff contract. Architecture sections 11 and 27 left IDs, layouts and transaction
details open; these choices were surfaced before implementation and documented
explicitly. There is no component-architecture deviation. The protocol has not yet
been validated against RP firmware because no RP/Pico is connected.

## Modes and configuration

The CLI now selects real GStreamer, Unix mux and RP I2C by default. Startup calls
GET_INFO and validates protocol 1 / eight modulators before any operational RP
command. The HTTP server remains available on communication or compatibility
failure, with DEGRADED status and a useful error. There is never automatic fallback.

| CLI mode | Sources | Mux | RP control |
|---|---|---|---|
| no mock flags | GStreamer | Unix client | Linux I2C |
| `--mock-rp` | GStreamer | Unix client | Original high-level mock |
| `--mock-rp --mock-mux` | GStreamer | Original mock | Original high-level mock |
| `--mock` | Original mock | Original mock | Original high-level mock |

Use **`--mock-rp` to continue the validated Milestone 3 setup without RP hardware**.
`--mock-mux` alone does not mock I2C. The embedding `create_app()` factory retains
its original mock defaults; inject the real client explicitly when embedding.

The existing persistent `SystemConfig.i2c_device` and
`SystemConfig.rp2354_i2c_address` select the hardware. CLI overrides are
`--i2c-device /dev/i2c-N` and `--rp-i2c-address 0xNN`. No address is assumed when
unset; startup reports hardware unavailable while serving the API. I2C configuration
changes take effect on daemon restart. `--i2c-timeout-ms` defaults to 200.

`rf_output_dbuv` is nominal configured/read-back QN output, not a measurement.
It remains null when RP cannot provide a meaningful nominal value. The existing
`rf_output_dbuv` is the nominal physical target; the RP owns QN PA conversion.
SQLite schema 2 replaces legacy raw `rf_level` values with an unset target (null),
because raw values cannot be reliably converted. Configure an explicit target
before real hardware use; frequency, assignments and desired enabled state are preserved.
The high-level mock continues accepting an unset target.

## Enable and inspect I2C on Raspberry Pi OS

These are setup instructions for hardware bring-up; they were not executed during
this milestone. Enabling the interface alone does not create an RP slave.

```sh
sudo apt update
sudo apt install i2c-tools
sudo raspi-config nonint do_i2c 0
sudo modprobe i2c-dev
sudo usermod -aG i2c "$(id -un)"
sudo reboot
```

Reconnect after reboot so group membership is refreshed. Inspect adapter discovery
and access (listing adapters does not probe attached slaves):

```sh
id
ls -l /dev/i2c-*
i2cdetect -l
ls /sys/class/i2c-dev/
```

Select the actual adapter from this list; `/dev/i2c-1` is a common example, not an
assumption inside the client. Raspberry Pi's [I2C configuration instructions](https://www.raspberrypi.com/documentation/computers/configuration.html#enable-or-disable-i2c)
and [raspi-config implementation](https://github.com/RPi-Distro/raspi-config/blob/master/raspi-config)
describe interface setup. The [Linux i2c-dev interface](https://docs.kernel.org/i2c/dev-interface.html)
documents device access and adapter discovery.

## Manual Pico 2 smoke test

First load firmware that implements [current V1 protocol](rp2354-protocol.md), including
the two-part response read. Connect SDA/SCL/common ground according to the board's
3.3 V wiring and pull-up design. Use the slave address configured in that firmware.
Keep `fmheadendd` stopped during direct client tests so two clients do not compete.
No QN register addresses, firmware pin assignments or slave address are invented here.

From the repository root, select the actual values interactively:

```sh
. "$HOME/venvs/fmheadend/bin/activate"
read -r -p 'I2C device path (for example /dev/i2c-1): ' RP_I2C_DEVICE
read -r -p 'Firmware 7-bit slave address (hex or decimal): ' RP_I2C_ADDRESS
export RP_I2C_DEVICE RP_I2C_ADDRESS
python - <<'PY'
import json, os
from fmheadendd.rp2354.linux_client import Rp2354Client
rp = Rp2354Client(os.environ['RP_I2C_DEVICE'], int(os.environ['RP_I2C_ADDRESS'], 0))
try:
    print(json.dumps(rp.get_info(), indent=2))
    print(json.dumps(rp.get_status(), indent=2))
finally:
    rp.close()
PY
```

Expect protocol version 1, firmware/hardware identifiers and eight channel records.
An incompatible version must be rejected. A NACK/device error must produce
HardwareUnavailable, never mock information. These two commands can validate the
RP protocol endpoint before QN hardware is attached; absent QN hardware must be
reported honestly in its status.

Once RP firmware can configure the attached QN, verify configuration does not enable
transmission. This explicitly selects 98100 kHz for the test; it is not a default:

```sh
python - <<'PY'
import os
from fmheadendd.rp2354.client import TxConfig
from fmheadendd.rp2354.linux_client import Rp2354Client
rp = Rp2354Client(os.environ['RP_I2C_DEVICE'], int(os.environ['RP_I2C_ADDRESS'], 0))
try:
    rp.get_info()
    rp.set_tx_enabled(0, False)
    for mode in ('DIGITAL', 'ANALOG'):
        rp.set_tx_config(0, TxConfig(frequency_khz=98100, rf_output_dbuv=96.0,
            stereo=True, preemphasis_us=50, audio_mode=mode, rds_enabled=False))
        status = rp.get_status()['modulators'][0]
        print(mode, status)
        assert status['tx_state'] == 'OFF', 'SET_TX_CONFIG unexpectedly enabled TX'
finally:
    rp.close()
PY
```

With RP but no working QN hardware, QN_ERROR is a meaningful failure of this second
step. Do not turn it into a successful hardware test. Any eventual TX enable test
must be an explicit `set_tx_enabled(0, True)` followed by a status check and disable;
configuration alone must leave TX off.

For the integrated API, start muxd independently as documented in
[Milestone 3](milestone3.md), then:

```sh
fmheadendd --database "$HOME/fmheadend-m4-smoke.db" --host 127.0.0.1 --port 8080 \
  --i2c-device "$RP_I2C_DEVICE" --rp-i2c-address "$RP_I2C_ADDRESS"
curl -sS http://127.0.0.1:8080/api/v1/system/info
curl -sS http://127.0.0.1:8080/api/v1/status
```

Run curl in another terminal. Verify real firmware info, `mocked=false`, actual
hardware TX state and separate RP/mux counters. Follow the Milestone 3 source/API
procedure to configure and start audio when QN hardware is ready. Stopping RP
communication must leave the HTTP server available and show UNRESPONSIVE/DEGRADED
with unknown hardware values; PCM behavior remains unchanged. When communication
returns, periodic status polling reopens and handshakes. Restoring desired RF state
automatically, RDS refill and broader system recovery remain later work.

`get_rds_fifo_free`, `push_rds`, `clear_rds`, register access, counter reset and soft reset are direct
client primitives with tests and documented payloads. No new debug REST surface,
RDS encoder, scheduler, refill policy, firmware, Web UI or systemd units were added.

## Reproduce validation

On the Pi from the repository root:

```sh
. "$HOME/venvs/fmheadend/bin/activate"
python -m pytest daemon/tests/test_rp2354.py -q -ra
MUXD_BINARY="$HOME/build/fmheadend-muxd-m3/fmheadend-muxd" \
  FMHEADEND_TEST_ALSA_DEVICE=plughw:2,0 \
  python -m pytest daemon/tests -q -ra
ctest --test-dir "$HOME/build/fmheadend-muxd-m3" --output-on-failure
```

The existing Linux venv and native mux build were reused over `ssh fmheadend`.
The Pi uses Raspberry Pi OS / Debian 13, Python 3.13.5 and native GStreamer/GI.

| Final suite | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Pi: new RP protocol/client tests | 151 | 0 | 0 |
| Pi: full Python regression (includes those 151) | 249 | 0 | 0 |
| Pi: unchanged native mux CTest | 8 | 0 | 0 |
| Windows: full Python regression | 229 | 0 | 20 |

Windows skips 15 native GStreamer tests, four Linux mux integration tests and the
Linux missing-device test. Two existing Starlette/AnyIO deprecation warnings remain.

RP packet exchange tests mock `os.open/read/write/close` and `fcntl` calls directly.
Pure serialization tests do not perform I/O. The Linux missing-device test uses real
OS calls and verifies an absent path raises HardwareUnavailable. No physical RP I2C
transaction succeeded or was claimed: the Pi had no `/dev/i2c-*` nodes and no attached
RP. Existing GStreamer, Behringer ALSA and native Unix mux integration ran for real,
with mock SPI. Successful physical I2C exchange, QN configuration and electrical/timing
validation remain pending the board and compatible firmware.
