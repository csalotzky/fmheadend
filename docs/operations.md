# FMheadend 1.0.0 operations guide

This guide covers normal operation of an installed FMheadend system. See the
[installation guide](installation.md) for deployment, the
[configuration reference](configuration.md) for startup settings, the
[Controller protocol](controller-protocol.md) for communication diagnostics, and
the [RP2354 firmware update guide](rp2354-firmware-update.md) for firmware work.

## 1. Services and normal startup

| Service | Role | Normal state |
|---|---|---|
| `fmheadend-muxd.service` | Receives PCM and sends framed audio to the Controller over SPI. | Running and configured by the backend. |
| `fmheadendd.service` | Controller communication, sources, RDS, REST API and WebUI. | Running; Dashboard reports `RUNNING` when dependencies are healthy. |

With systemd services installed, start both:

```sh
sudo systemctl start fmheadend-muxd.service
sudo systemctl start fmheadendd.service
```

Check status and logs:

```sh
systemctl status fmheadend-muxd.service fmheadendd.service
journalctl -u fmheadend-muxd.service -u fmheadendd.service -b
```

Restart only the affected service:

```sh
sudo systemctl restart fmheadendd.service
```

```sh
sudo systemctl restart fmheadend-muxd.service
```

For planned shutdown, stop the backend first so it can request RF disablement,
then stop the mux:

```sh
sudo systemctl stop fmheadendd.service
sudo systemctl stop fmheadend-muxd.service
```

The backend unit starts after and wants the mux unit, but neither service is a
hard prerequisite for the other. Their normal independent-start behavior is:

| Event | Expected behavior |
|---|---|
| Mux starts first | It waits for a verified physical TX count and sends no SPI frames. |
| Backend starts first | It reports the mux unavailable until the control socket appears, then configures the mux after Controller capability negotiation. |
| Backend restarts | A matching running mux configuration is retained; source and Controller recovery still determine whether transmissions can resume. |
| Mux restarts | The backend observes the loss, reports degraded status, and reapplies the verified TX count when the mux returns. |
| Controller unavailable at startup | The backend does not guess a TX count or configure the mux for one. |

After any restart, use the Dashboard and the System page before relying on a
transmission. A Controller communication failure during shutdown means software
cannot prove that RF disablement reached the Controller.

## 2. Managing transmissions

Use each Dashboard **TX** card's **START** and **STOP** button for an individual
Modulator. The card shows the requested action until a later Controller
observation confirms the resulting RF state.

| Term | Meaning |
|---|---|
| Saved enablement | The persistent `enabled` selection for a Modulator. |
| Desired state | Saved enablement, physical availability, and the absence of the global STOP ALL hold. |
| Reported state | The latest Controller observation (`ON`, `OFF`, `ERROR`, or `UNKNOWN`), not a promise that a command just succeeded. |

An individual **START** persists enablement before attempting the lifecycle.
Starting requires a physically available TX, valid TX configuration, Controller
and mux readiness, and either analog input or a usable digital Source. A Source
that is still preparing keeps RF off; the Dashboard can show a pending or source
error while the backend waits for readiness.

An individual **STOP** clears the Modulator's saved enablement and requests RF
off. If Controller confirmation is unavailable, the saved state is still OFF but
the physical state remains unconfirmed until a later observation.

FMheadend stores eight logical Modulator configurations, while the Controller
reports the physical count. Only physical IDs `TX0` through `TX(N-1)` appear in
the public inventory and can be controlled. When verified capacity decreases,
absent configurations are retained but their saved enablement is cleared. They
remain OFF if they reappear until an operator enables them again.

## 3. STOP ALL and START ALL

The shell header provides **STOP ALL** and **START ALL**. These commands have
different effects from editing individual enablement:

| Action | Persistent result | Hardware action |
|---|---|---|
| **STOP ALL** | Sets `global_stopped=true`; keeps each Modulator's `enabled` selection. | Requests RF OFF for every physical TX and releases active audio resources. |
| **START ALL** | Sets `global_stopped=false`; keeps the existing per-Modulator selections. | Attempts only enabled, physically available, sufficiently configured Modulators. |
| Individual **START** while STOP ALL is active | Clears the hold and makes the selected TX the saved resume selection; other logical Modulators are disabled. | Attempts that selected TX only. |

`global_stopped` is persistent. A backend or system restart does not resume RF
while the STOP ALL hold remains active. START ALL does not force an unavailable,
disabled, unconfigured, or source-blocked Modulator on; inspect the returned card
state and any bulk-operation notice for skipped or failed TXs.

## 4. Status and monitoring

The Dashboard is the primary operational view. Its header shows overall health,
Controller state, mux state, and the number of reported ON transmitters. TX cards
show configuration, reported RF state, Source state, digital-audio flow, and RDS
state. The System page shows Controller identity/capabilities, mux transport and
counters, Controller audio-link errors, and recent backend log entries.

| Interface | Use | Interpretation |
|---|---|---|
| Dashboard | Routine health and TX operation | `RUNNING` means current backend checks found no system-level fault; a Source error remains visible on its affected TX. |
| System page | Component diagnostics | Mux transport and Controller identity are observed values; they may be retained after a later disconnection. |
| `GET /api/v1/status` | Cached overall and TX status | Does not perform live I/O. Use `snapshot_stale` and Controller observation fields to judge its age. |
| `GET /api/v1/system/info` | Version, verified capabilities, frequency-plan information | Retained capability data does not prove that the Controller is currently connected. |
| `GET /api/v1/sources/{id}/status` | Cached status for one Source | Includes source state, diagnostic fields, and observation age. |
| `GET /api/v1/system/logs` | Recent in-process backend logs | Contains the newest 100 backend log entries since backend startup; use the journal for service history. |

`Status delayed` or `Status outdated` in the header means the latest published
observation is old. It is distinct from an HTTP failure. `BACKEND UNAVAILABLE`
means the browser has not received a valid status response for its failure grace
period. A stale Controller observation is not evidence that the Controller is
healthy or unhealthy by itself.

## 5. Source readiness and failures

Sources are shared by every TX that selects the same Source. A failure of an
external input is reported on that Source and its TX cards; it does not by itself
make Controller or mux communication unavailable.

| Source condition | What the operator sees | First action |
|---|---|---|
| HTTP, UDP, RTP, podcast, or ALSA input unavailable | Source `CONNECTING`, `BUFFERING`, or `ERROR` state on the affected TX. | Check the endpoint, network, capture device, and source-specific error text. |
| Folder or playlist still being scanned | `LOCAL_DISCOVERY_PENDING`; requested RF remains OFF until playable media is prepared. | Wait for discovery, then inspect path/mount access and logs if it does not complete. Do not repeatedly press START. |
| Empty, unreadable, or invalid LOCAL input | Source error with the resolver/parser diagnostic. | Correct the path, mount, playlist, or permissions; test the Source configuration before starting the TX. |
| GStreamer or Audio Profile failure | Source error or pipeline diagnostic. | Inspect the profile and `fmheadendd` journal; confirm required GStreamer plugins are installed. |

A STOP issued while LOCAL discovery is pending removes the pending start. A later
discovery result must not activate that TX. A terminal LOCAL startup failure clears
the affected saved enablement; a deferred preparation state does not mean audio is
ready or that RF has been enabled.

Running input loss is handled as a Source condition. Review the affected TX card,
source status, and journal before changing RF settings or restarting services.

## 6. Controller and mux failures

| Event | Observable result | Operator response |
|---|---|---|
| Controller communication lost | Dashboard becomes degraded and Controller observation becomes unavailable or stale. Saved configuration and last verified capability information are retained. | Check Controller power, I2C connection, configured adapter/address, then inspect `fmheadendd` logs. Do not infer RF state from saved enablement. |
| Controller communication returns | The backend re-establishes Controller information and resumes its bounded recovery work. | Wait for fresh status and verify the reported state of each required TX. |
| Capability negotiation fails | Controller-dependent lifecycle commands remain blocked; the mux is not configured from a guessed count. | Resolve the Controller/protocol problem before retrying TX control. |
| Mux control socket unavailable | Mux state is `ERROR`; the system is degraded and no current SPI-streaming claim is valid. | Check `fmheadend-muxd.service`, its journal, runtime-directory settings, and transport configuration. |
| Mux reconnects | The backend compares mux state with the verified Controller count and configures or resumes it when safe. | Confirm `RUNNING`, the expected transport, and fresh TX status on the System page. |

Controller recovery deliberately does not treat a transient communication failure
as permission to replay every configuration or change saved enablement. Likewise,
a retained `spidev` or `mock` transport label after a mux disconnection is the last
observed transport, not proof that the mux remains connected. The **SPI MOCK**
indicator appears only after mock transport has been observed.

## 7. Troubleshooting

| Symptom | First useful check | Next step |
|---|---|---|
| Backend or mux service failed | `systemctl status fmheadendd.service fmheadend-muxd.service` | Read `journalctl -u` output for the failing service and correct its startup setting. |
| Dashboard shows Controller unavailable | System page and `GET /api/v1/status` | Check I2C device/address and Controller power; see the [Controller protocol](controller-protocol.md). |
| Mux is `ERROR` or no SPI frames advance | System page: mux state, transport, errors, and frame counters | Check `fmheadend-muxd.service`, its runtime directory, SPI device, and mux CLI settings in [Configuration](configuration.md). |
| TX refuses to START | TX card source/error text and saved TX configuration | Ensure the TX is physical, enabled, frequency-configured, and has ready analog or digital audio. |
| `LOCAL_DISCOVERY_PENDING` persists | Source status and `fmheadendd` journal | Verify the mount/path is responsive and readable; test the Source configuration. |
| Source has no audio | Source state, Digital Audio state, and PCM counters on its TX card | Check input reachability/device availability and required GStreamer elements or Audio Profile plugins. |
| RDS is suspended or not progressing | TX card RDS state and Controller/mux health | Restore Controller readiness first; then inspect the TX RDS configuration. |
| WebUI or API unavailable | `systemctl status fmheadendd.service` and backend journal | Check the configured listen address/port and optional authentication settings. |
| Firmware recovery is required | Confirm service and Controller status first | Follow the [RP2354 firmware update guide](rp2354-firmware-update.md); do not substitute an ad-hoc flash procedure. |

Before restarting services or changing persistent settings, capture the relevant
Dashboard/System-page state and recent journal messages. This preserves the error
context needed to distinguish a Source problem from Controller or mux failure.
