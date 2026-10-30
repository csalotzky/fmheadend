# FMheadend 1.0.0 WebUI user guide

The WebUI is served by `fmheadendd` and uses the same REST API as external
clients. Open the backend HTTP address in a browser. If HTTP Basic
Authentication is enabled, the browser displays its native credentials prompt.

## 1. Interface overview

The navigation sidebar provides five areas:

| Page | Purpose |
|---|---|
| **Dashboard** | Monitor and operate the physical transmitters. |
| **Sources** | Create, test and maintain audio inputs. |
| **Modulators** | Configure RF, audio-input and RDS settings for each physical TX. |
| **Audio Profiles** | View bundled processing presets and manage custom profiles. |
| **System** | Inspect backend, Controller and mux diagnostics and recent logs. |

The header remains visible on every page. It summarizes overall state,
Controller and mux health, the reported TX ON count, CPU load and temperature.
Its action button shows **STOP ALL** during normal operation and **START** while
the persistent global stop hold is active. This **START** has global START ALL
semantics: it resumes eligible, previously selected Modulators rather than
enabling every available TX.

## 2. Dashboard

<!-- Screenshot TODO: images/webui/dashboard.png — Dashboard with populated TX cards and visible system status. -->

The Dashboard contains one card for each transmitter reported by the Controller.
FMheadend retains eight logical Modulator configurations, but the page never
creates cards for unavailable physical channels.

Each TX card combines saved configuration with live observations:

| Card area | Information |
|---|---|
| Heading | TX number and reported `ON`, `OFF`, `ERROR` or `UNKNOWN` state. |
| Main readout | Frequency, Source, PS, RadioText and station identifiers when configured. |
| Configuration | Stereo or mono, pre-emphasis, RF output and audio-input details. |
| Runtime | Source state, digital-audio flow, Audio Profile and RDS state. |
| Controls | **START** or **STOP**, plus a link to the Modulator editor. |

**START** saves the selected Modulator as enabled and begins preparation. The
card may show a requested state while FMheadend waits for Source readiness and a
later Controller observation. A successful HTTP response alone is not proof of
RF output. **STOP** clears saved enablement and requests RF off; if Controller
communication is unavailable, the physical outcome remains unconfirmed.

When the global stop hold is active, starting one TX clears the hold and makes
that TX the sole saved resume selection. Other saved Modulator selections are
disabled. **STOP ALL** instead preserves the individual selections, turns on the
persistent hold and requests all physical transmitters off. The header's
**START** action invokes the global START ALL behavior: it clears that hold and
attempts the eligible, previously selected Modulators; it does not force
unavailable, unselected or incomplete channels on.

The header separates data freshness from HTTP availability. **Status delayed**
and **Status outdated** retain the last observations while identifying old data.
**BACKEND UNAVAILABLE** appears only after sustained HTTP communication failure.
A stale or retained Controller value is not proof of current connectivity.

## 3. Sources

The **Sources** page groups entries as **Internet stream (HTTP)**,
**Local media**, **Podcast**, **Network stream (UDP)**,
**Network stream (RTP)** and **Sound card (ALSA)**. Groups are collapsible and
show their Source count. Entries within a group are sorted by name.

Each row shows the input summary, Audio Profile selection, runtime state and the
number of TX references. Available actions are **Edit**, **Test** and **Delete**.
A Source referenced by a Modulator cannot be deleted.

<!-- Screenshot TODO: images/webui/sources.png — Source editor with a representative input configuration and its test controls. -->

Choose **Add source**, enter a unique **Name**, then select **Source type**. The
editor exposes only the controls relevant to that type:

| Type | Main UI controls |
|---|---|
| Internet stream | **Stream URL**, **Browse stations**, **Test stream** and buffering settings. |
| UDP / RTP | Receive address, port, multicast and format or codec settings; **Test source**. |
| Local media | Folder or Playlist mode, playback order, server path, **Browse...**, recursion and **Test source**. |
| Podcast | Feed list, **Test feeds**, scheduling mode, refresh interval and replay cooldown. |
| Sound card | Capture device, **Refresh devices**, custom ALSA device, channel selection and **Test source**. |

Test actions use the unsaved form values. They do not save the Source, start a
transmitter or prove RF operation. Local-media testing counts discovered entries;
actual decoding occurs during playback. Podcast results retain per-feed details
and may report partial success.

The **Audio processing** section selects either
**System default (<profile name>)** or a fixed Audio Profile and provides
**Source Gain (dB)**. **Advanced source options** contains expert GStreamer
property overrides. The optional RDS section stores Source-owned station data.

Saving an audio-affecting change rebuilds the active shared Source pipeline, so
all Modulators using it may briefly lose PCM together. RDS-only changes do not
restart audio. Unsaved edits are protected by a discard confirmation.

See [Audio Sources](sources.md) for type-specific settings and playback behavior.

### 3.1 Example: internet stream to a transmitter

1. Open **Sources**, choose **Add source**, and select
   **Internet stream (HTTP)**.
2. Enter **Name** and **Stream URL**. Use **Test stream** to confirm decoded audio;
   **Browse stations** can locate a Radio Browser entry.
3. Select an **Audio Profile**, then save the Source.
4. Open **Modulators**, choose the required TX, select
   **FMheadend audio**, and select the new **Source**.
5. Save the Modulator, return to **Dashboard**, and use **START**.
6. Wait for Source, digital-audio and reported TX state to confirm readiness.

## 4. Modulator configuration

The **Modulators** page lists only the Controller-reported physical TX inventory.
Choose **Edit TX N** to open **Edit Modulator (TX N)**. The status block above
the form shows reported TX state, selected Source, Source state and digital-audio
status without replacing the saved form values.

| Section | Principal controls |
|---|---|
| RF | Frequency, **RF output** (28 nominal attenuation levels from 0–40.5 dB in 1.5 dB steps relative to maximum), Stereo/Mono and 50/75 microsecond pre-emphasis. |
| Audio | **FMheadend audio** or **External line input**, Source or analog gain, **Gain control** and **Soft clipping**. |
| RDS | **RDS enabled**, custom settings or **Follow source** for digital audio. |

External line input belongs directly to that transmitter and bypasses the Source
pipeline. With FMheadend audio, a saved Source assignment is required before the
TX can produce digital audio. Saving hardware-affecting changes may stop and
resume a running TX; the result notice identifies the completed or failed stage.
Transmission enablement itself is controlled from the Dashboard.

## 5. Audio Profiles

<!-- Screenshot TODO: images/webui/audio-profiles.png — Audio Profile editor with several DSP stages expanded. -->

The list identifies the **System default**, shows how many Sources use each
profile and separates read-only bundled profiles from writable custom profiles.

- Choose **View** for a bundled profile or **Clone** to create an editable copy.
- Choose **Add Audio Profile** for a new custom profile.
- Use **Edit** and **Delete** for custom profiles; the current default and profiles
  referenced by Sources cannot be deleted.
- Choose **Set as default** to update the inherited profile immediately.

In the editor, enter **Name** and **Description**, then expand the numbered DSP
stages. Select an **Implementation**, or **Off** where supported, and edit the
available parameters.
Missing native implementations are marked unavailable instead of being silently
bypassed. **Save** reports whether active affected Sources were rebuilt
successfully.

A Source set to **System default (<profile name>)** follows later default changes.
A Source set to a named profile remains fixed to it. For processing order and
parameter details, see [Audio processing and Audio Profiles](audio.md).

### 5.1 Example: customize processing

1. Open **Audio Profiles** and **Clone** the closest bundled profile.
2. Name the custom copy, edit only the required stages, and select **Save**.
3. Use **Set as default** to apply it to Sources that inherit the default, or
   select it explicitly in a Source's **Audio Profile** field.
4. Review the save result and the affected Source states before relying on audio.

## 6. RDS

<!-- Screenshot TODO: images/webui/rds.png — RDS editor with PI, PTY, PS, RadioText and AF settings visible. -->

RDS is edited inside a Source or Modulator rather than on a separate page.
The shared editor provides **PI**, optional **ECC**, **Programme type**, **Traffic
programme (TP)**, PS entries, Alternative Frequencies and RadioText settings.
The US region displays RBDS programme-type names; other regions use RDS names.

A Source can enable **Configure RDS for this source**. **Fill from source** fills
PS and default RadioText from the Source name without changing PI. On a digital
Modulator, **RDS configuration source** can use **Custom** values or
**Follow source**. Following a Source keeps the RDS content owned by that Source;
the UI links back to its editor.

PS supports multiple timed entries. RadioText supports a default value, CR or
PAD termination and optional Source metadata. RDS edits are persistent. The
Dashboard shows the effective PS, RadioText and RDS runtime state, but Source
readiness and RDS health do not by themselves confirm RF output.

### 6.1 Example: configure station RDS

1. Edit the Source and enable **Configure RDS for this source**.
2. Set PI, programme type, PS entries and the default RadioText, then save.
3. Edit the Modulator, enable RDS and set **RDS configuration source** to
   **Follow source**.
4. Save, then inspect the Dashboard card's effective RDS content and state.

## 7. System and diagnostics

The **System** page presents diagnostic data in four areas:

| Area | Information |
|---|---|
| Backend | Status, application version and media backend. |
| Controller | Communication state, protocol, firmware, hardware target and mock indication. |
| Multiplexer | State, observed transport, transport state and frames sent. |
| FM frequency plan | Active region, FM band and frequency step. |

**Diagnostics** lists I2C, SPI and Controller audio-link errors, the last
Controller error and per-TX counters. **Recent backend logs** shows the newest 100
in-process entries since backend startup; use **Refresh logs** to reload them.
Unavailable or retained values must be read together with current component and
freshness state. A retained transport or Controller identity is not a live-health
confirmation.

For service recovery and fault isolation, see [Operations](operations.md).
Startup settings are covered in [Configuration](configuration.md), and external
automation uses the [REST API](api.md).
