# FMheadend installation

FMheadend runs as two services on a Raspberry Pi:

| Component | Purpose |
|---|---|
| `fmheadendd` | Audio sources, processing, Controller communication, API and WebUI |
| `fmheadend-muxd` | PCM transport to the Controller over SPI |

The WebUI is served by `fmheadendd`; Node.js is needed to **build** it, not to run it. Controller firmware installation is covered in the [firmware update guide](rp2354-firmware-update.md).

## 1. Automatic installer — planned

A shell installer is planned for Raspberry Pi OS. It will download prebuilt components from **GitHub Releases**, install dependencies and configure the services, including the recommended FMheadend GStreamer plugins.

**Not available yet.** There is no installer URL or supported `curl` command. The manual procedure below describes the current source-based installation.

## 2. Requirements

| Item | Requirement |
|---|---|
| Computer | Raspberry Pi with a 64-bit Linux environment and usable I²C/SPI interfaces |
| OS | **Raspberry Pi OS Lite (64-bit)** is the primary target; other Linux distributions are not yet installation-tested |
| Python | 3.11 or later |
| Controller | FMheadend Controller connected through I²C and SPI |
| Optional sources | Network, local storage and/or ALSA devices as needed |

Development currently takes place on a **Raspberry Pi 3B**. Testing on Pi 4 and Zero 2 W is planned; Pi 5 has not been tested. Actual capacity depends on the number of active channels and DSP workload, not just I²C/SPI compatibility.

Enable the appropriate I²C and SPI interfaces in Raspberry Pi OS before installing. Confirm the resulting device names (`/dev/i2c-*`, `/dev/spidev*`) on the target system.

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
  gstreamer1.0-libav gstreamer1.0-alsa alsa-utils \
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

Both commands should display the corresponding plugin information without errors.

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
| `/opt/fmheadend/webui` | Built WebUI |
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
sudo apt install -y curl
curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/nodesource_setup.sh
sudo -E bash /tmp/nodesource_setup.sh
sudo apt install -y nodejs

node --version
npm --version
```

The required versions are Node.js >=24.15.0 <25 and npm >=11 <13.

**Build and install the WebUI:**

```sh
cd ~/fmheadend/webui
npm ci
npm run build

sudo install -d -m 0755 /opt/fmheadend/webui
sudo cp -a dist/fmheadend-ui/browser/. /opt/fmheadend/webui/
```

Verify that `/opt/fmheadend/webui/index.html` exists.

### 5.4 Bundled Audio Profiles

```sh
cd ~/fmheadend
sudo install -d -m 0755 /usr/share/fmheadend/audio-profiles/default
sudo install -m 0644 audio-profiles/default/*.json /usr/share/fmheadend/audio-profiles/default/
```

The bundled profiles are separate from the Python package. The default-profile directory must be present and readable by the backend.

## 6. Configure the services

Install the supplied systemd units:

```sh
cd ~/fmheadend
sudo install -m 0644 systemd/fmheadend-muxd.service systemd/fmheadendd.service /etc/systemd/system/
```

Create `/etc/fmheadend/muxd.env`:

```ini
MUXD_ARGS="--transport spidev --spi-device /dev/spidev0.0 --spi-clock-hz 25000000 --runtime-dir /run/fmheadend"
```

Create `/etc/fmheadend/fmheadendd.env`:

```ini
FMHEADEND_ARGS="--database /var/lib/fmheadend/fmheadend.db --host 0.0.0.0 --port 8080 --i2c-device /dev/i2c-1 --controller-i2c-address 0x62 --mux-runtime-dir /run/fmheadend --webui-dir /opt/fmheadend/webui --audio-profiles-default-dir /usr/share/fmheadend/audio-profiles/default --audio-profiles-custom-dir /var/lib/fmheadend/audio-profiles/custom"
```

Adjust the I²C/SPI device paths and Controller address for the actual installation. `--transport spidev` uses real hardware; for a software-only check, set `--transport mock` explicitly in `MUXD_ARGS`.

**Network security:** the example binds HTTP to all interfaces (`0.0.0.0`). Restrict network access or use authentication behind HTTPS/a trusted encrypted connection. Basic Authentication is optional (`--passwd-file`); never expose credentials over unencrypted public HTTP.

Enable and start:

```sh
sudo systemd-analyze verify /etc/systemd/system/fmheadend-muxd.service /etc/systemd/system/fmheadendd.service
sudo systemctl daemon-reload
sudo systemctl enable --now fmheadend-muxd.service fmheadendd.service
```

The mux service waits for the backend to supply the physical transmitter count before sending SPI audio.

## 7. Verify the installation

```sh
systemctl status fmheadend-muxd.service fmheadendd.service
journalctl -u fmheadend-muxd.service -u fmheadendd.service -b
curl -fsS http://127.0.0.1:8080/api/v1/status
curl -fsS http://127.0.0.1:8080/api/v1/system/info
```

Open `http://<pi-address>:8080/` to access the WebUI when network access is configured. Create Sources and configure Modulators before starting transmitters. A new database does not automatically enable RF output.

## 8. Troubleshooting

| Problem | Check |
|---|---|
| Python cannot import `gi` / `Gst` | OS GStreamer packages and virtual environment created with `--system-site-packages` |
| Backend installation fails to build native code | Compiler, Python headers and build dependencies |
| WebUI does not load | `/opt/fmheadend/webui/index.html` and `--webui-dir` |
| Controller unavailable | I²C address, device, permissions, connection and firmware |
| SPI transfer fails | Device, access rights, SPI configuration and sufficient `spidev` buffer size (up to 4,100 bytes per packet) |
| Factory Audio Profile cannot run | `fmheadendlimiter`, `fmheadendclipper` and any other plugins required by that profile |
| Services fail to start | `journalctl` output, environment files, paths and service account |

For protocol diagnostics, see the [Controller protocol](controller-protocol.md). For SWD provisioning or I²C firmware updates, see the [RP2354 firmware update guide](rp2354-firmware-update.md).

The manual installation procedure is undergoing clean-system validation. The automated GitHub Release installer is not yet available.