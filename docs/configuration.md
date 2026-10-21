# FMheadend 1.0.0 configuration reference

| Configuration type | Defined in | Persistence |
|---|---|---|
| Backend startup options | `fmheadendd` CLI or `FMHEADEND_ARGS` service environment | Effective for one process lifetime |
| Mux startup options | `fmheadend-muxd` CLI or `MUXD_ARGS` service environment | Effective for one process lifetime |
| System settings | SQLite, WebUI, `PATCH /api/v1/system/config` | Database |
| Sources and Modulators | SQLite, WebUI and REST API | Database |
| Audio Profiles | Default and custom JSON profile directories | Filesystem |

## 1. Backend CLI — `fmheadendd`

Set startup options directly on the command line or in the `FMHEADEND_ARGS`
value read by `fmheadendd.service`. Changing an option requires restarting the
backend service.

### 1.1 HTTP, database and logging

| Option | Default | Description |
|---|---|---|
| `--database PATH` | `/var/lib/fmheadend/fmheadend.db` | SQLite configuration database. Its parent directory is created when needed. |
| `--host ADDRESS` | `0.0.0.0` | HTTP listen address. |
| `--port PORT` | `8080` | HTTP listen port; must be an integer accepted by Uvicorn. |
| `--log-level LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `--webui-dir PATH` | Disabled | Static Angular WebUI directory. It must contain `index.html`; without it the backend is API-only. |
| `--passwd-file [PATH]` | Disabled | Enables HTTP Basic Authentication. A bare option uses `/var/lib/fmheadend/htpasswd`; an explicit path uses that file. |

### 1.2 Controller communication

| Option | Default | Description |
|---|---|---|
| `--i2c-device PATH` | `/dev/i2c-1` | Linux I²C adapter used for Controller communication. |
| `--controller-i2c-address ADDRESS` | Required for real Controller mode | 7-bit Controller address in decimal or `0x` hexadecimal form. No address is assumed. |
| `--i2c-timeout-ms MS` | `200` | I²C adapter transaction timeout in milliseconds. |
| `--controller-status-poll-interval-ms MS` | `1000` | Positive Controller status-poll interval in milliseconds. |
| `--region CODE` | `1` | Region behavior: `0` Expert, `1` Europe, `2` United States, `3` Japan. |

`--region` selects WebUI frequency choices and RDS programme-type labels: the
United States uses RBDS PTY labels, while the other regions use RDS labels.
Expert choices use verified Controller frequency capabilities when available.
When a new database is initialized, United States creates Modulators with 75 µs
preemphasis; the other regions use 50 µs. Changing the region later does not
rewrite saved preemphasis or frequencies, and does not replace hardware
frequency validation.

### 1.3 PCM and mux connection

| Option | Default | Description |
|---|---|---|
| `--mux-runtime-dir PATH` | `/run/fmheadend` | Directory containing mux PCM sockets and `mux-control.sock`. Must match muxd's `--runtime-dir`. |
| `--pcm-staging-buffer-ms MS` | `200` | Per-Source staging capacity / burst headroom. Positive integer milliseconds. |
| `--pcm-staging-max-age-ms MS` | `100` | Maximum retained age in the staging queue. Positive integer milliseconds. |
| `--pcm-consumer-buffer-ms MS` | `300` | Per-consumer PCM capacity and maximum age. Positive integer milliseconds. |

### 1.4 Media, profiles and diagnostics

| Option | Default | Description |
|---|---|---|
| `--audio-profiles-default-dir PATH` | `/usr/share/fmheadend/audio-profiles/default` | Read-only bundled Audio Profile directory. |
| `--audio-profiles-custom-dir PATH` | `/var/lib/fmheadend/audio-profiles/custom` | Persistent writable custom Audio Profile directory. |
| `--media-browser-root PATH` | None; repeatable | Additional absolute root exposed by the LOCAL source path browser. The filesystem root is rejected. |
| `--audio-telemetry-socket PATH` | Disabled | Local Unix socket for optional NDJSON audio diagnostics. |
| `--audio-telemetry-hz HZ` | `50` | Diagnostic sample rate, from `10` to `100` Hz. Has no effect without `--audio-telemetry-socket`. |

The LOCAL browser always also offers `/mnt`, `/media`, and `/srv` when those
directories are available. Browser roots restrict browsing only; persisted LOCAL
Source paths are configured separately.

### 1.5 Development and testing

| Option | Default | Description |
|---|---|---|
| `--mock` | Off | Uses mock Sources, mux and Controller control. |
| `--mock-controller` | Off | Uses the high-level mock Controller client; media and mux remain normal. |
| `--mock-mux` | Off | Uses normal media Sources with the mock mux. |

Mock options are for development or isolated checks. They are not a fallback for
an unavailable real Controller or SPI transport.

## 2. Mux CLI — `fmheadend-muxd`

Set mux options directly or in the `MUXD_ARGS` value read by
`fmheadend-muxd.service`. Mux settings require a muxd restart.

| Option | Default | Description |
|---|---|---|
| `--transport mock\|spidev` | Required | Selects the transport. No transport is selected implicitly. |
| `--runtime-dir PATH` | `/run/fmheadend` | Runtime directory for PCM and control Unix sockets. |
| `--spi-device PATH` | `/dev/spidev0.0` | Linux SPI device for `spidev` transport. |
| `--spi-clock-hz HZ` | `25000000` | Positive SPI clock in Hz for `spidev`; accepts an unsigned 32-bit integer. |
| `--spi-mode MODE` | `0` | SPI mode `0` through `3`. |
| `--ring-buffer-ms MS` | `300` | Positive PCM ring capacity per channel in milliseconds. |
| `--capture FILE` | Disabled | Bounded mock-transport frame capture file. Invalid with `spidev`. |
| `--capture-frames COUNT` | `375` | Mock-capture frame limit, `1` through `100000`. |
| `--help` | — | Prints usage. |
| `--version` | — | Prints the mux version. |

The mux starts in `WAITING_FOR_CONFIG`; it sends no SPI frames until the backend
has negotiated Controller capabilities and configured the physical TX count.

## 3. Persistent system configuration

`SystemConfig` contains exactly two database fields.

| Setting | Default | Purpose | Changed by | Effect |
|---|---|---|---|---|
| `default_audio_profile_id` | `default/basic_filtering.json` | Profile inherited by Sources that follow the system default. | Audio Profiles WebUI, or `PATCH /api/v1/system/config` | Saved immediately. Active Sources following the default are reconfigured; stopped Sources remain stopped. |
| `global_stopped` | `false` | Persistent STOP ALL hold. | Global STOP/START controls | STOP ALL sets it; START ALL clears it and considers each eligible, enabled Modulator. |

`default_audio_profile_id` must be a valid logical profile path such as
`default/basic_filtering.json` or `custom/name.json`. Sources with
`use_default_audio_profile=true` resolve it dynamically; Sources with a selected
`audio_profile_id` do not change when the system default changes.

STOP ALL preserves each Modulator's saved `enabled` selection while preventing
startup/recovery from restoring RF. START ALL clears the hold and starts only
physical Modulators whose saved configuration is eligible to start.

Sources, Modulators, RDS profiles and podcast history are separate SQLite
entities. The database always retains eight logical Modulator records; Controller
capabilities determine the currently available physical TX inventory.

## 4. Files and directories

| Purpose | Application default | Notes |
|---|---|---|
| SQLite database | `/var/lib/fmheadend/fmheadend.db` | Configured by `--database`. |
| Bundled Audio Profiles | `/usr/share/fmheadend/audio-profiles/default` | Configured by `--audio-profiles-default-dir`; missing defaults are an error. |
| Custom Audio Profiles | `/var/lib/fmheadend/audio-profiles/custom` | Configured by `--audio-profiles-custom-dir`; created lazily. |
| WebUI assets | None | Enabled only by `--webui-dir`; `/opt/fmheadend/webui` is an installation-guide example. |
| Mux sockets | `/run/fmheadend` | Configured independently by backend `--mux-runtime-dir` and mux `--runtime-dir`. |
| Password file | None; bare `--passwd-file` selects `/var/lib/fmheadend/htpasswd` | Independent of the custom-profile directory. |
| Audio telemetry socket | None | Created only by `--audio-telemetry-socket`; its adjacent `.owner` marker is managed by the daemon. |
| Service argument files | `/etc/fmheadend/fmheadendd.env`, `/etc/fmheadend/muxd.env` | Paths required by the shipped systemd units; the shell-style variables are `FMHEADEND_ARGS` and `MUXD_ARGS`. |

Default profiles are read-only. Custom profiles use logical paths of the form
`custom/name.json`; profile files are not database rows.

`fmheadendlimiter` and `fmheadendclipper` are separately distributed custom
GStreamer plugins. The backend can start without them, but they are strongly
recommended for a functional installation: many bundled Audio Profiles require
them and cannot run without them. See [Installation](installation.md) for the
available installation guidance.

## 5. WebUI and API configuration

| Area | Interface | Persistence and effect |
|---|---|---|
| Sources | Sources editor / source API | Saved in SQLite. Active-source changes follow the Source lifecycle rules; configuration alone does not start a TX. |
| Modulators | Modulator editor / modulator API | Saved in SQLite. Start and Stop control desired enablement; physical availability comes from the Controller. |
| Audio Profiles | Audio Profiles editor / profile-file API | Default files are read-only; custom profile edits are saved as JSON. Changes to a profile are applied through the existing affected-Source reconfiguration path. |
| System default profile | Audio Profiles **Set as default** / `PATCH /api/v1/system/config` | Saved immediately and dynamically inherited by eligible Sources. |
| STOP ALL hold | Dashboard global controls / modulator bulk API | Saved immediately as `global_stopped`. |
| System page | WebUI System page | Read-only diagnostics; it does not edit startup arguments. |

Controller device/address, HTTP listener, logging, region, mux runtime path,
PCM buffers, WebUI assets, authentication, browser roots and telemetry are
startup settings. Change their CLI or service-environment values and restart the
owning service. SPI device, clock, mode and mux ring capacity belong to muxd,
not the backend or WebUI.

## 6. Authentication and security

`--passwd-file` protects the complete HTTP application: REST API, OpenAPI,
static WebUI assets and SPA routes. The file must be a non-empty, regular,
Apache-compatible htpasswd file containing valid bcrypt entries. Missing,
unreadable or invalid files cause startup to fail; later file failures deny
requests until corrected.

Authentication is disabled when the option is omitted. Password verification is
bounded and cached briefly; password-file changes invalidate cached credentials.
Use HTTPS or another trusted encrypted transport. HTTP Basic Authentication over
plain HTTP does not protect credentials from interception.

## 7. Short configuration examples

### 7.1 Service environment values

```ini
# /etc/fmheadend/muxd.env
MUXD_ARGS="--transport spidev --runtime-dir /run/fmheadend"
```

```ini
# /etc/fmheadend/fmheadendd.env
FMHEADEND_ARGS="--controller-i2c-address 0x62 --webui-dir /opt/fmheadend/webui"
```

The two runtime-directory values must match. Add paths or hardware-specific
arguments only where their corresponding CLI table requires them.

For service layout and installation prerequisites, see
[Installation](installation.md). Controller protocol details and Controller
capability semantics are documented in [Controller protocol](controller-protocol.md).
