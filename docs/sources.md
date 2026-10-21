# Audio Sources

An FMheadend **Source** is a saved audio-input configuration. A digital-input Modulator selects one Source; the backend starts that Source when the Modulator needs audio and distributes its processed PCM to every Modulator using it.

Source configuration is persistent. Decoder state, connection state, playback position and PCM queues are runtime state. Saving a Source does not start a transmission.

| Concept | Behavior |
|---|---|
| Saved Source | Stores the input, Audio Profile, gain and optional source RDS settings in SQLite. |
| Active Source | Exists while at least one Modulator consumes it. |
| Shared Source | One decoder and processing pipeline can feed multiple Modulators. Each consumer has its own bounded PCM queue. |
| Source readiness | Indicates whether usable audio is available; it does not prove that RF is on. |
| Analog input | A Modulator input mode that bypasses Sources and the host PCM path. It is not a Source type. |

## 1. Source types

| WebUI type | Input | Principal settings | Notes |
|---|---|---|---|
| Internet stream (HTTP) | HTTP or HTTPS media stream | URL, buffer, connection timeout | Format support depends on the installed GStreamer elements. HLS works when the required demuxer and decoder are present. |
| Network stream (UDP) | Headerless PCM carried in UDP datagrams | Bind address, port, optional multicast group, PCM format, sample rate, channels | The sender's PCM parameters must match the configuration. |
| Network stream (RTP) | RTP carrying PCM, MP3, AAC or Opus | Address, port, optional multicast group, codec, payload type and codec-specific fields | Uses an RTP jitter buffer before depayloading and decoding. |
| Local media | A directory or an M3U playlist visible to the backend host | Mode, playback order, server path, recursive folder scan | Discovery selects candidates; actual decodability is established during playback. |
| Podcast | One or more RSS or Atom feeds with media enclosures | Feeds, scheduling mode, refresh interval, replay cooldown | Feed discovery and episode history are managed independently from ordinary HTTP streams. |
| Sound card (ALSA) | An ALSA capture PCM device | Device, channel selection | The device is opened and converted through GStreamer to the common output format. |

### 1.1 HTTP streams

Enter the final media URL in **Stream URL**. FMheadend uses GStreamer's URI decoder, then converts the result to the common PCM format.

| Setting | Default | Meaning |
|---|---:|---|
| Buffer | `3000 ms` | Input buffering requested for the stream. |
| Connection timeout | `10000 ms` | Time allowed for connection and useful audio progress; runtime supervision constrains it to 1–30 seconds. |

**Browse stations** searches [Radio Browser](https://www.radio-browser.info/), a free, community-maintained directory of internet radio stations from around the world. FMheadend integrates its station catalog to make discovering and configuring streams easier. Selecting a result stores its station identifier and resolved URL. For a linked station, FMheadend can resolve a changed stream URL after repeated runtime failures; a replacement URL is persisted only after it produces decoded PCM.

Stream and codec support follows the GStreamer installation. A reachable web address is insufficient if its content cannot be demultiplexed and decoded.

### 1.2 UDP

UDP accepts raw PCM without a container or transport header. Configure the sender's exact sample format, sample rate and channel count. Supported sample formats are `S16LE`, `S16BE`, `S24LE`, `S24BE`, `S32LE`, `S32BE`, `F32LE`, `F32BE` and `U8`; channel count may be 1–8.

**Host** is the receive address, **Port** is 1–65535, and **Multicast group** is optional. FMheadend does not negotiate the PCM parameters with the sender.

### 1.3 RTP

RTP supports the following payload families:

| Codec selection | Additional requirements |
|---|---|
| PCM | Set the RTP clock rate and channel count; payload is interpreted as L16 PCM. |
| MP3 | Uses the RTP MP3 depayloader and a 90 kHz RTP clock. |
| AAC | Set clock rate, channels and hexadecimal codec data. |
| Opus | Uses the Opus RTP depayloader and a 48 kHz RTP clock. |

**Host** is the receive address, **Port** is 1–65535, and **Multicast group** is optional. The RTP payload type is 0–127. FMheadend does not negotiate these parameters with the sender.

### 1.4 Local media

Local media has two modes:

| Mode | Path refers to | Discovery behavior |
|---|---|---|
| Folder | An absolute directory on the backend host | Selects supported files in that directory and, when enabled, its subdirectories. |
| Playlist | An absolute `.m3u` or `.m3u8` file on the backend host | Preserves parsed entry order and duplicate entries. |

There is no separate single-file mode. To play selected files, list them in an M3U playlist.

#### 1.4.1 Folder discovery

Folder mode recognizes candidates with these extensions: `.mp3`, `.flac`, `.wav`, `.ogg`, `.opus`, `.m4a` and `.aac`. Hidden entries are skipped. Subdirectories are scanned only when **Include subfolders** is enabled; directory symlinks are not followed. Folder entries use a natural path sort before playback ordering is applied.

The file extension is a discovery filter, not a decoder guarantee. Permissions must allow the backend service user to traverse the directory and read each media file.

#### 1.4.2 M3U playlists

M3U parsing uses the following rules:

- Blank lines and lines beginning with `#` are ignored.
- Relative filesystem entries resolve against the playlist's directory.
- Absolute paths remain absolute.
- HTTP and HTTPS entries are accepted.
- Other URI schemes are rejected.
- Percent-encoded entries are decoded once.
- Duplicate entries are retained and can therefore play more than once per cycle.
- Discovery does not verify that every referenced file or URL is currently reachable or decodable.

An unreadable playlist produces `PLAYLIST_UNREADABLE`; an unsupported entry produces `INVALID_PLAYLIST_ENTRY`. An empty resolved playlist is not playable.

#### 1.4.3 Playback order and refresh

**Sequential** playback repeats the discovered folder order or M3U order. **Random** playback shuffles the complete catalog, plays every entry once per cycle, and generates a new order for the next cycle. When alternatives exist, consecutive cycles do not repeat the boundary item.

Discovery runs in the background and publishes complete catalog snapshots. A refresh never alters the active cycle; new files become eligible at a later cycle boundary, and there is no filesystem watcher. If a refresh is pending or fails, playback can reuse the last valid snapshot. Removed or inaccessible entries can still fail when selected.

During initial discovery the Source reports `LOCAL_DISCOVERY_PENDING` and the requested Modulator remains RF off. Stopping the Modulator cancels its pending start, so a later result cannot activate it. Discovery concurrency is bounded, and Source shutdown does not wait indefinitely for a blocked filesystem call.

FMheadend imposes no configured media-count limit. Very large folders and playlists require memory for a complete catalog and playback cycle.

### 1.5 Podcasts

A Podcast Source accepts 1–32 unique HTTP or HTTPS RSS/Atom feed URLs. Each feed may set an optional maximum episode age and maximum episode count. Only entries with a usable media enclosure become candidates.

| Setting | Default | Behavior |
|---|---:|---|
| Schedule mode | `Smart` | Prefers eligible unplayed and less-recently played episodes while distributing selections across feeds. |
| Feed refresh interval | `30 minutes` | Schedules feed-catalog refresh without interrupting the current episode. |
| Replay cooldown | `24 hours` | Avoids recently played episodes when alternatives exist; the scheduler relaxes the cooldown if every candidate is inside it. |

**Random** scheduling uses a shuffled bag of episode identities: each eligible episode is selected once before a new bag is generated, with boundary-repeat avoidance when alternatives exist. Removed episodes are pruned; newly discovered episodes enter a subsequent bag.

Feed downloads are bounded by request time, redirect and response-size limits. A failed feed is reported separately. If it has a previous successful catalog, that catalog remains available while other feeds can refresh normally. A Source with no eligible episodes stays idle until a later scheduled refresh can supply one.

Playback history is stored per Source, feed and episode. An episode is marked played after 60 seconds of decoded PCM, not when it is merely selected; an episode that ends before that threshold is not marked played. Testing feeds does not change history. Deleting the Source deletes its history, while removing a feed from the Source does not erase that feed's historical records.

Episode and feed titles can be exposed as Source metadata for downstream RadioText behavior. Successful feed parsing does not prove that an enclosure can subsequently be downloaded or decoded.

### 1.6 ALSA capture

Choose a discovered device or enter a custom ALSA PCM name. **Refresh devices** updates the list; it does not test capture. **Test source** opens the configured device and waits for PCM.

Capture must be available as 48 kHz mono or stereo audio. Channel selection can keep stereo, convert mono, or select the left or right input. A raw `hw:` device must directly support the requested format; an ALSA `plughw:` name can provide ALSA-side conversion where appropriate.

Only one process can open many hardware PCM devices at a time. An ALSA busy error usually means another application or another Source owns the device.

## 2. Common settings

Every Source has a unique name and the following audio settings:

| Setting | Behavior |
|---|---|
| Audio Profile | `System default (<profile name>)` follows the current system default dynamically. Selecting a named profile fixes the Source to that profile. |
| Source Gain | Applies gain within the Source pipeline before its PCM is distributed. The default is `0 dB`; any finite value up to and including `+20 dB` is accepted, with no configured lower bound. |
| Configure RDS | Stores Source-owned RDS defaults and metadata behavior. It does not change audio decoding. |
| Advanced GStreamer properties | Expert overrides for a restricted set of source, queue and resampler properties. Invalid elements or properties are rejected. |

New Sources follow the system-default Audio Profile. Changing that system default restarts active Sources that follow it; Sources fixed to another profile are unaffected. Editing the contents of an in-use profile similarly restarts the active Sources that resolve to it.

The separately distributed `fmheadendlimiter` and `fmheadendclipper` GStreamer plugins are required by many bundled Audio Profiles. A Source using such a profile fails to start if the elements are unavailable. See [Installation](installation.md).

## 3. Creating, editing and assigning a Source

1. Open **Sources** and choose **Add Source**.
2. Select a **Source type** and enter its type-specific settings.
3. Select an **Audio Profile** and adjust **Source Gain** if required.
4. Use the available test action before saving when the input can be probed safely.
5. Save the Source.
6. Open a Modulator, select **FMheadend audio**, and assign the Source.

The editor tests the unsaved values shown in the form. A test does not save the Source, change podcast playback history, start a Modulator or prove RF transmission.

| Source type | Test action | What success proves |
|---|---|---|
| HTTP | **Test stream** | The stream opened and produced decoded PCM. Codec, sample rate and channel information are reported when available. Detected station metadata can fill the Source name. |
| UDP, RTP, ALSA | **Test source** | The configured input produced decoded PCM within the test interval. |
| Local media | **Test source** | Folder or playlist discovery returned at least one candidate. Files are not decoded by this test. |
| Podcast | **Test feeds** | Feed parsing found at least one eligible episode. Per-feed failures and partial success remain visible. Audio is not played. |

Saving an audio-affecting edit to an active Source replaces its shared pipeline while retaining its consumers. All Modulators using that Source experience the same restart. RDS-only edits do not restart its audio pipeline. A Source referenced by a Modulator cannot be deleted.

## 4. Processing and sharing

GStreamer performs input, demultiplexing, decoding, format conversion and Audio Profile processing in the backend. Every digital Source produces 48 kHz, signed 16-bit little-endian stereo PCM.

The pipeline belongs to the Source, rather than to an individual Modulator. Multiple Modulators selecting the same active Source share its playback position, metadata, gain and Audio Profile processing. Each Modulator receives the processed PCM through an independent bounded consumer queue, so one delayed consumer does not create an ever-growing backlog for the others.

The backend sends each consumer's PCM through its own mux socket. `fmheadend-muxd` maintains the fixed transmitter slots and sends multiplexed PCM to the Controller. See [Architecture](architecture.md) for the full path.

## 5. Status and troubleshooting

The Sources page shows the saved configuration and current Source state. Modulator cards also expose their selected Source and Source state. Common runtime states include `IDLE`, `CONNECTING`, `BUFFERING`, `RUNNING` and `ERROR`; detailed diagnostics can include codec, input sample rate, channels, output caps, metadata, reconnect count and the last error.

Transient input failures use bounded retry delays while consumers remain attached. A Source failure is normally isolated to that Source. For an already enabled digital Modulator, loss of PCM yields silence through the mux path; it does not by itself prove that RF has stopped.

| Symptom or message | Meaning | First action |
|---|---|---|
| Source remains `CONNECTING` or `BUFFERING` | No usable decoded PCM has arrived yet. | Run the Source test and inspect its detailed error and codec information. |
| `LOCAL_DISCOVERY_PENDING` | Initial folder or playlist discovery is still running. | Wait for discovery; verify host path access if it does not complete. Do not repeatedly press START. |
| `FOLDER_UNREADABLE` | The backend cannot enumerate all required directories. | Check the path, mount state and service-user traverse/read permissions. |
| `PLAYLIST_UNREADABLE` | The M3U file cannot be opened or decoded as a playlist. | Check its path, UTF-8 text and permissions. |
| `INVALID_PLAYLIST_ENTRY` | An entry has an unsupported or malformed path/URI. | Correct the indicated playlist line. |
| No supported files or no playlist entries | Discovery returned an empty catalog. | Check extensions, recursion and playlist contents. |
| Network test times out | No decodable PCM arrived with the configured parameters. | Check reachability, codec, RTP payload settings or raw UDP format. |
| ALSA device is missing or busy | Device discovery or capture open failed. | Refresh devices; check the PCM name and whether another process owns it. |
| Missing GStreamer element | A decoder, transport element or Audio Profile plugin is unavailable. | Install the required GStreamer component or select a profile supported by the installation. |
| Podcast feed shows partial failure | At least one feed failed while another was usable. | Expand the feed details and correct only the failed URL or eligibility settings. |
| Source is `RUNNING`, but TX is not on | Source readiness and RF state are independent. | Check the Modulator's desired state, Controller observation and mux status. |

For service-level recovery and transmission controls, see [Operations](operations.md). Startup paths and persistent locations are documented in [Configuration](configuration.md) and [Installation](installation.md).
