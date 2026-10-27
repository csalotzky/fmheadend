# FMheadend installation

FMheadend runs as two services on a Raspberry Pi:

| Component | Purpose |
|---|---|
| `fmheadendd` | Audio sources, processing, Controller communication, API and WebUI |
| `fmheadend-muxd` | PCM transport to the Controller over SPI |

The WebUI is served by `fmheadendd`; Node.js is needed to **build** it, not to run it. Controller firmware installation is covered in the [firmware update guide](rp2354-firmware-update.md).

## 1. Interactive installer
Download the `fmheadend-webui` artifact from [Build WebUI](https://github.com/csalotzky/fmheadend/actions/workflows/build-webui.yml) and extract `fmheadend-webui.tar.gz`.
Run `sudo ./install.sh --webui-archive /path/to/fmheadend-webui.tar.gz` from the FMheadend source directory (also for upgrades).

## 2. Requirements

| Item | Requirement |
|---|---|
| Computer | Raspberry Pi with a 64-bit Linux environment and usable I²C/SPI interfaces |
| OS | **Raspberry Pi OS Lite (64-bit)** is the primary target; other Linux distributions are not yet installation-tested |
| Python | 3.11 or later |
| Controller | FMheadend Controller connected through I²C and SPI |
| Optional sources | Network, local storage and/or ALSA devices as needed |

Development currently takes place on a **Raspberry Pi 3B**. A fresh Pi 4
installation has succeeded; Zero 2 W and Pi 5 still need performance testing.
Actual capacity depends on active channels and DSP workload, not just I²C/SPI
compatibility. Only Pi 2 variants capable of running the required 64-bit OS are
eligible for this installer.

## 3. System dependencies

For Raspberry Pi OS Lite / Debian-based installations:

```sh
sudo apt update
sudo apt install git build-essential cmake pkg-config \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  python3-dev python3-venv \
  python3-gi python3-gst-1.0 gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav gstreamer1.0-alsa alsa-utils i2c-tools \
  lsp-plugins-lv2
```

The Python environment must be able to import the OS-provided PyGObject/GStreamer bindings. Node.js **24.x** (`>=24.15.0`) and npm **11–12** are additionally required when building the Angular WebUI from source.

### 3.1 FMheadend GStreamer plugins

FMheadend uses two custom GStreamer audio processing plugins:

- **`fmheadendlimiter`** — FM preemphasis-aware lookahead limiter.
- **`fmheadendclipper`** — FM preemphasis-aware final peak clipper.

These plugins are not included in the standard GStreamer packages. Although the FMheadend backend can start without them, many bundled Audio Profiles depend on them. **Installing both is recommended for a normal FMheadend setup.**

The plugins are maintained in a [separate GitHub repository](https://github.com/csalotzky/fmheadend-gst-plugins).

**Build and install:**

```sh
cd ~
git clone https://github.com/csalotzky/fmheadend-gst-plugins.git
cd fmheadend-gst-plugins

make -j"$(nproc)" plugin
make test
make integration-test
sudo make install
```

**Verify installation:**

```sh
gst-inspect-1.0 fmheadendlimiter >/dev/null 2>&1 && echo "Limiter: OK" || echo "Limiter: FAILED"
gst-inspect-1.0 fmheadendclipper >/dev/null 2>&1 && echo "Clipper: OK" || echo "Clipper: FAILED"
```

The commands print `Limiter: OK` and `Clipper: OK` when the plugins are available;
otherwise they print the corresponding `FAILED` result.

For additional information, see the [plugin documentation](https://github.com/csalotzky/fmheadend-gst-plugins#readme).


## 4. Service account and directories

The supplied systemd units use a dedicated `fmheadend` account. On a fresh Raspberry Pi OS installation:

```sh
sudo groupadd --system --force fmheadend
id fmheadend >/dev/null 2>&1 || \
  sudo useradd --system --gid fmheadend --home-dir /var/lib/fmheadend \
    --no-create-home --shell /usr/sbin/nologin fmheadend
sudo usermod -aG i2c,spi,audio fmheadend
sudo install -d -o fmheadend -g fmheadend -m 0750 /var/lib/fmheadend
sudo install -d -o fmheadend -g fmheadend -m 0750 /var/lib/fmheadend/audio-profiles/custom
sudo install -d -m 0755 /opt/fmheadend /etc/fmheadend
```

Confirm that the device groups exist and that the service account can access the actual I²C, SPI and ALSA devices. Group ownership and SPI/I²C device names depend on the OS configuration.

| Path | Purpose |
|---|---|
| `/opt/fmheadend/venv` | Installed Python backend |
| `/usr/share/fmheadend/webui` | Built WebUI |
| `/usr/share/fmheadend/audio-profiles/default` | Bundled, read-only Audio Profiles |
| `/var/lib/fmheadend` | SQLite database and custom profiles |
| `/run/fmheadend` | Runtime sockets, created by the mux service |
| `/etc/fmheadend` | Service environment files |

## 5. Install FMheadend from source

Download the FMheadend source code from the [official GitHub repository](https://github.com/csalotzky/fmheadend):

```sh
cd ~
git clone https://github.com/csalotzky/fmheadend.git
cd fmheadend
```

The source code is now available in `~/fmheadend`. Follow the steps below to build and install each component.

### 5.1 Backend

```sh
cd ~/fmheadend
sudo python3 -m venv --system-site-packages /opt/fmheadend/venv
sudo /opt/fmheadend/venv/bin/python -m pip install ./daemon
```

Verify that the virtual environment can access the system GStreamer bindings:

```sh
/opt/fmheadend/venv/bin/python -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
```

The backend executable is `/opt/fmheadend/venv/bin/fmheadendd`. Its installation also builds the native LV2 lifecycle component.

### 5.2 PCM mux daemon

```sh
cd ~/fmheadend
cmake -S muxd -B build/muxd -DCMAKE_BUILD_TYPE=Release
cmake --build build/muxd --parallel
sudo cmake --install build/muxd
/usr/local/bin/fmheadend-muxd --help
```

The commands assume the default CMake installation prefix (`/usr/local`).

### 5.3 WebUI

The WebUI requires Node.js only when building from source. Prebuilt releases do not require Node.js or npm on the target device.

**Install Node.js 24.x and npm (source builds only):**

```sh
sudo apt install curl
curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/nodesource_setup.sh
sudo -E bash /tmp/nodesource_setup.sh
sudo apt install nodejs

node --version
npm --version
```

The required versions are Node.js >=24.15.0 <25 and npm >=11 <13.

**Build and install the WebUI:**

```sh
cd ~/fmheadend/webui
npm ci
npm run build

sudo install -d -m 0755 /usr/share/fmheadend/webui
sudo cp -a dist/fmheadend-ui/browser/. /usr/share/fmheadend/webui/
```

Verify that `/usr/share/fmheadend/webui/index.html` exists.

### 5.4 Bundled Audio Profiles

```sh
cd ~/fmheadend
sudo install -d -m 0755 /usr/share/fmheadend/audio-profiles/default
sudo install -m 0644 audio-profiles/default/*.json /usr/share/fmheadend/audio-profiles/default/
```

The bundled profiles are separate from the Python package. The default-profile directory must be present and readable by the backend.

### 5.5 Optional: Password protection

Authentication is optional and disabled by default. The password tool prompts for
a username (default `admin`), then requests and confirms the password with hidden input:

```sh
sudo /opt/fmheadend/venv/bin/python ~/fmheadend/tools/create_passwd.py \
  --file /var/lib/fmheadend/htpasswd
sudo chown root:fmheadend /var/lib/fmheadend/htpasswd
sudo chmod 0640 /var/lib/fmheadend/htpasswd
```

`--user` and `--password` remain available for explicit values; only omitted values
are prompted. Interactive entry requires a controlling terminal, including when
stdin is redirected. Existing files require explicit `--overwrite` to replace.

Authentication protects both the WebUI and REST API. HTTP Basic Authentication does
not encrypt credentials; use HTTPS for access across untrusted networks.

## 6. Configure I²C and SPI

FMheadend requires I²C and SPI to be enabled, with the SPI transfer buffer increased
to 8192 bytes. The supplied `tools/setup_pi.py` utility configures these settings
automatically.

After cloning the FMheadend repository as described in section 5, run:

```sh
cd ~/fmheadend
sudo python3 tools/setup_pi.py
```

**A reboot is required for the configuration changes to take effect:**

```sh
sudo reboot
```

The utility updates `/boot/firmware/config.txt` and `cmdline.txt`, configures automatic
loading of `i2c-dev`, creates backups, enables SPI0 and sets `spidev.bufsiz=8192`.
On Pi Zero/Zero 2 W and Pi 1–4, dynamic core clock changes can make the actual SPI
clock differ from the requested rate. The setup utility sets
[`core_freq_fixed=1`](https://www.raspberrypi.com/documentation/computers/config_txt.html#overclocking)
to keep the core clock fixed at the platform's higher frequency. Pi 5 uses RP1 for
SPI and does not require this setting; its core clock configuration is left unchanged.
For testing, `--keep-core-clock` leaves all existing core clock settings untouched
and adds none, even with `--overwrite-existing`. It does not undo an existing
`core_freq_fixed=1` setting.

Its model-dependent I²C defaults are:

| Raspberry Pi | I²C configuration | Expected device |
|---|---|---|
| Pi 3, Pi 4, Zero/Zero 2 W and other supported pre-Pi-5 models | Software I²C on GPIO2/3, bus 8, 2 µs delay | `/dev/i2c-8` |
| Pi 5 | Hardware I²C at 100 kHz | `/dev/i2c-1` |

Certain Broadcom I²C controllers have a [documented clock-stretching issue](https://github.com/raspberrypi/linux/issues/254), also [reported on Raspberry Pi 4](https://github.com/raspberrypi/linux/issues/4884). During FMheadend testing, hardware I²C caused intermittent communication errors on Raspberry Pi 3 and Pi 4. Software I²C is therefore used as a temporary workaround until the issue is investigated further.

The software-I²C delay is a timing parameter, not an exact guaranteed clock rate.

Existing active I²C, SPI or `spidev.bufsiz` settings cause a safe failure. Review
the reported entries, then rerun with `--overwrite-existing` to replace only the
relevant settings. On Pi Zero–4, this also replaces existing global `core_freq`,
`core_freq_min` and `core_freq_fixed` entries with one `core_freq_fixed=1` line.
Relevant settings in conditional sections require manual review; the utility
stops without changing files. An unidentified model with an explicit I²C mode
leaves core clock settings unchanged. Useful overrides are:

```sh
sudo python3 tools/setup_pi.py --i2c-mode hw --i2c-speed 100000 --overwrite-existing
sudo python3 tools/setup_pi.py --i2c-mode sw --i2c-bus 8 --i2c-delay-us 2 --overwrite-existing
```

FMheadend requires an SPI buffer of at least **4100 bytes**; the Linux default of 4096 bytes is insufficient. The setup utility configures 8192 bytes. Use `--keep-spi-bufsiz` only if an adequate buffer size is already configured.

After reboot, verify the I²C device printed by the utility, SPI0 and the SPI buffer
size:

```sh
# Pi 3/4 and supported older models
ls -l /dev/i2c-8 /dev/spidev0.0

# Pi 5 or explicit hardware I²C mode
ls -l /dev/i2c-1 /dev/spidev0.0

cat /sys/module/spidev/parameters/bufsiz
```

The final command should print `8192`.

On Pi 3/4, `/dev/i2c-8` should be owned by `root:i2c` with `0660` permissions.
The `i2c-tools` package supplies the standard device permission rule; no manual
`modprobe`, ownership, permission or custom udev setup is required.

If either device is missing, list the available interfaces:

```sh
ls -l /dev/i2c-* /dev/spidev*
```

Select interfaces that match the Controller's physical wiring. SPI bus and
chip-select numbers are significant: `/dev/spidev0.0` and `/dev/spidev0.1` are
not interchangeable.

## 7. Configure the services

Install the supplied systemd units:

> **Important:** On Raspberry Pi, the mux daemon requires realtime scheduling (`SCHED_FIFO`, priority 50). Without it, SPI audio packets may occasionally be delivered too late, causing audio glitches. The supplied systemd service configures this automatically.

```sh
cd ~/fmheadend
sudo install -m 0644 systemd/fmheadend-muxd.service systemd/fmheadendd.service /etc/systemd/system/
```

Create `/etc/fmheadend/muxd.env`:

```ini
MUXD_ARGS="--transport spidev --spi-device /dev/spidev0.0 --spi-clock-hz 25000000 --runtime-dir /run/fmheadend"
```

Create `/etc/fmheadend/fmheadendd.env` using exactly one of these alternatives.

Without authentication (default):

```ini
FMHEADEND_ARGS="--host 0.0.0.0 --port 8080 --i2c-device /dev/i2c-8 --region 1 --webui-dir /usr/share/fmheadend/webui"
```

With authentication, if section 5.5 was completed:

```ini
FMHEADEND_ARGS="--host 0.0.0.0 --port 8080 --i2c-device /dev/i2c-8 --region 1 --webui-dir /usr/share/fmheadend/webui --passwd-file /var/lib/fmheadend/htpasswd"
```

`--region`: `0` Expert, `1` Europe (default), `2` United States, `3` Japan; see the
[configuration reference](configuration.md#12-controller-communication) for details.

Make sure `--i2c-device` matches the I²C interface configured in Chapter 6. By default, the `setup_pi.py` utility configures `/dev/i2c-8` on Pi 3/4 and `/dev/i2c-1` on Pi 5. If you changed the I²C configuration, adjust this path accordingly.

The `--transport spidev` setting uses real hardware; for a software-only check,
set `--transport mock` explicitly in `MUXD_ARGS`.

**Network security:** the example binds HTTP to all interfaces (`0.0.0.0`). Restrict
network access and use HTTPS or another trusted encrypted connection when the service
is accessible across an untrusted network.

Enable and start:

```sh
sudo systemd-analyze verify /etc/systemd/system/fmheadend-muxd.service /etc/systemd/system/fmheadendd.service
sudo systemctl daemon-reload
sudo systemctl enable --now fmheadend-muxd.service fmheadendd.service
```

The mux service waits for the backend to supply the physical transmitter count before sending SPI audio.

## 8. Verify the installation

```sh
systemctl status fmheadend-muxd.service fmheadendd.service
journalctl -u fmheadend-muxd.service -u fmheadendd.service -b
```

Verify the REST API:

Without authentication:
```sh
curl -fsS http://127.0.0.1:8080/api/v1/status
```

With authentication:
```sh
curl -fsS --user YOUR_USERNAME http://127.0.0.1:8080/api/v1/status
```

Open `http://<pi-address>:8080/` to access the WebUI. Create Sources and configure Modulators before starting transmitters.

## 9. Troubleshooting

| Problem | Check |
|---|---|
| Python cannot import `gi` / `Gst` | OS GStreamer packages and virtual environment created with `--system-site-packages` |
| Backend installation fails to build native code | Compiler, Python headers and build dependencies |
| WebUI does not load | `/usr/share/fmheadend/webui/index.html` and `--webui-dir` |
| Controller unavailable | I²C address, device, permissions, connection and firmware |
| SPI transfer fails | Device, access rights, SPI configuration and sufficient `spidev` buffer size (up to 4,100 bytes per packet) |
| Factory Audio Profile cannot run | `fmheadendlimiter`, `fmheadendclipper` and any other plugins required by that profile |
| Services fail to start | `journalctl` output, environment files, paths and service account |

For protocol diagnostics, see the [Controller protocol](controller-protocol.md). For SWD provisioning or I²C firmware updates, see the [RP2354 firmware update guide](rp2354-firmware-update.md).

The manual installation procedure and refined interactive installer still need
broader clean-system qualification. Automatic GitHub Release downloads are not
implemented.
