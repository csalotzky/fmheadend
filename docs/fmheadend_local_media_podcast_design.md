# FMHeadend Local Media and Podcast Source Design

**Status:** Design / implementation reference  
**Primary near-term scope:** Local file playback (`playlist` and `folder` modes)  
**Future architectural scope:** Podcast/RSS source, metadata-driven now-playing/RDS, crossfade  
**Audience:** FMHeadend developers and coding agents working in the repository

---

## 1. Purpose

FMHeadend currently supports digital input sources such as HTTP/HLS, UDP, RTP, and ALSA. These sources ultimately feed the existing digital audio path and GStreamer-based ingest/playback infrastructure.

This document defines the design for two related source families:

1. **Local media playback**
   - Playlist mode: play a fixed `.m3u` / `.m3u8` file.
   - Folder mode: discover playable files in a directory, optionally recursively.

2. **Podcast/RSS playback**
   - A future source type that aggregates episodes from multiple RSS feeds.
   - Supports random playback and a smarter radio-style rotation scheduler.
   - Reuses the same general media-item and playback abstractions as local media.

The local-media feature is the implementation priority. The podcast design is intentionally documented now so that local-media abstractions do not unnecessarily prevent later reuse.

The implementation must preserve existing FMHeadend architectural behavior instead of introducing an independent playback model.

---

## 2. Existing Architectural Constraints

### 2.1 Shared source means shared playback

FMHeadend already treats a configured source as a shared playback instance when multiple transmitters consume it.

Local media must preserve this behavior:

- One configured source owns one playback instance.
- It owns one queue/order/current position.
- Multiple TX consumers receive the same resulting audio stream through the existing fan-out behavior.
- Enabling a second TX on the same source must **not** start an independent shuffle or independent track position.

If a user wants two independent random rotations of the same folder, the supported solution is to configure **two separate sources** pointing at the same folder.

This behavior is intentional and must not be special-cased for local media.

### 2.2 Playback state is transient unless explicitly stated otherwise

For local media:

- Current track is not persisted.
- Current playback position is not persisted.
- Current shuffle order is not persisted.
- Queue state is not persisted.

When the source genuinely stops and later starts again, its list and queue are rebuilt from scratch.

Podcast playback is different only in one specific respect: the future SMART scheduler needs persistent per-episode play history. The podcast queue and current playback position remain transient.

---

## 3. Core Design Principle: Resolver -> Selection/Queue -> Player

Playlist parsing, folder scanning, ordering, scheduling, and GStreamer playback must remain separate responsibilities.

Conceptually:

```text
Source-specific resolver
        |
        v
Available media items
        |
        v
Queue builder / scheduler
        |
        v
next MediaItem
        |
        v
GStreamer playback adapter
        |
        v
Existing FMHeadend audio path
```

The playlist/folder layer must not directly manage GStreamer state.

The GStreamer layer must not own playlist semantics.

### 3.1 General media-item representation

Do not lock the internal queue abstraction to filesystem paths.

Use an internal media-item concept that can represent both local files and remote media:

```text
MediaItem
  uri
  optional identity
  optional title
  optional artist
  optional collection/show
  optional published_at
  optional source-specific metadata
```

The exact language-level type should follow existing repository conventions, but the conceptual separation matters.

Examples:

```text
file:///mnt/music/album/track.flac
https://cdn.example.net/podcast/episode-123.mp3
```

The resolver may work with filesystem paths internally, but by the time an item is handed to the playback adapter it should be expressible as a media URI.

---

# Part I — Local Media Source

## 4. Local Media Source Model

Local media is one logical source family with two user-facing modes:

```text
mode = playlist
mode = folder
```

Both modes share:

- playback order selection;
- infinite broadcast-style looping;
- the same playback adapter;
- the same error-handling model;
- the same shared-source semantics.

Suggested logical configuration shape:

```json
{
  "type": "local",
  "mode": "folder",
  "path": "/mnt/nas/music",
  "recursive": true,
  "order": "random"
}
```

Playlist example:

```json
{
  "type": "local",
  "mode": "playlist",
  "path": "/srv/fmheadend/playlists/night.m3u",
  "order": "sequential"
}
```

The exact enum/type names may be adapted to the repository's existing naming conventions. The semantic model should remain the same.

---

## 5. API Path Semantics

### 5.1 Store host filesystem paths

The configuration/API must store an **absolute path on the FMHeadend host**.

Examples:

```text
/home/pi/music
/srv/fmheadend/media
/mnt/nas/music
/media/pi/MUSIC_USB
```

Do not store GStreamer `file://` URIs in source configuration.

The API owns filesystem paths. The playback adapter owns path-to-URI conversion.

Example conversion boundary:

```text
/mnt/nas/music/foo.flac
        |
        v
file:///mnt/nas/music/foo.flac
        |
        v
GStreamer
```

### 5.2 Keep absolute paths long term

Absolute paths are not a temporary debug format. They remain the backend representation even if a future web UI hides them behind a browser or named media roots.

This keeps:

- configuration direct and debuggable;
- CLI/API use simple;
- mounted NAS/USB storage naturally supported;
- the backend independent of frontend navigation UX.

### 5.3 USB and NAS storage

FMHeadend should not implement its own mount manager.

If the operating system exposes media through a readable mountpoint, local playback may use it.

Examples:

```text
/mnt/nas
/mnt/music-usb
/media/pi/SANDISK
```

For appliance-style installations, stable mountpoints are recommended, especially for removable storage. For example, administrators may mount a USB filesystem by UUID at:

```text
/mnt/music-usb
```

Filesystem type is outside the source feature. ext4, FAT, NFS, CIFS, etc. are equivalent from FMHeadend's point of view if the process can read the resulting path.

---

## 6. Web UI — Initial Version

The initial web UI should remain deliberately simple.

### 6.1 Common fields

- Source mode:
  - Playlist
  - Folder
- Path
- Playback order:
  - Sequential
  - Random

### 6.2 Folder-only field

- Include subfolders / Recursive

### 6.3 No file browser in v1

The first version should expose a normal text field containing the absolute server-side path.

Example:

```text
Folder
[/mnt/nas/music________________________]
```

This is appropriate for a server/appliance administration UI and avoids prematurely introducing:

- filesystem browser APIs;
- permission/navigation complexity;
- `/proc`, `/sys`, and other unsafe filesystem exposure;
- symlink traversal policy in the UI;
- container/namespace confusion;
- mount discovery behavior.

A future file browser may expose only configured allowed roots such as:

```text
/srv/fmheadend/media
/mnt
/media
```

but selection should still resolve to the same absolute-path backend configuration.

### 6.4 Save-time path validation

Do **not** require the configured path to exist at save time.

Valid scenarios include:

- NAS temporarily offline;
- USB drive unplugged;
- mount not yet active during configuration.

A frontend may optionally show a non-blocking warning, but save must remain possible.

Actual usability is determined when the source starts.

---

## 7. Playlist Mode

### 7.1 Supported playlist format

Support:

- `.m3u`
- `.m3u8`

The implementation should focus on the path/URL list semantics, not extended metadata semantics.

### 7.2 Entry forms

Playlist entries may be:

- absolute local filesystem paths;
- relative local filesystem paths;
- HTTP/HTTPS media URLs.

Relative local paths are resolved relative to the directory containing the playlist file.

Example:

```text
#EXTM3U
../music/intro.mp3
/mnt/archive/interview.flac
https://cdn.example.org/programmes/hour-2.mp3
```

### 7.3 `#EXTINF` and other metadata

FMHeadend should **not** use `#EXTINF` as authoritative now-playing metadata.

For this feature, M3U is a playback list, not a metadata database.

Rules:

- blank lines are ignored;
- lines beginning with `#` are treated as comments/directives and ignored for playback purposes;
- unknown M3U directives are ignored;
- `#EXTINF` metadata is not consumed for title/artist logic.

Later metadata should come from media tags or source-native metadata, with filename fallback for local files.

### 7.4 Duplicate entries

Duplicate entries in a user-authored playlist must be preserved.

If the same item appears multiple times, assume the user intended it.

Do not deduplicate M3U entries.

---

## 8. Folder Mode

Folder mode discovers media files from a configured directory.

### 8.1 Recursive scan

Configuration:

```text
recursive = false | true
```

When disabled, only direct children are considered.

When enabled, subdirectories are scanned.

### 8.2 Playable extension filtering

Do not attempt to probe/decode every file during directory scanning.

Instead:

1. Filter by a conservative allowlist of common audio extensions supported by the deployed GStreamer stack.
2. Let GStreamer determine actual decodability when playback occurs.

The exact allowlist should follow the codecs/plugins available in the project environment. Typical candidates include:

```text
.mp3
.flac
.wav
.ogg
.opus
.m4a
.aac
```

The list should be case-insensitive.

An extension match means "candidate for playback", not "guaranteed decodable".

### 8.3 Hidden entries

Ignore hidden dotfiles and hidden dot-directories during folder discovery.

Examples to ignore:

```text
.Trash
.Spotlight-V100
.hidden.mp3
```

This avoids accidental playback of filesystem/NAS metadata and reduces scan noise.

### 8.4 Symlink policy

The configured top-level source path may itself be a symlink.

Do not rewrite the stored configured value to its `realpath()` result.

Example:

```text
/srv/fmheadend/music -> /mnt/nas/radio-music
```

The user-configured value should remain:

```text
/srv/fmheadend/music
```

For recursive traversal:

- symlinked directories should not be followed;
- this prevents loops and accidental escape into unrelated filesystem trees;
- symlinked files may be accepted if they resolve to readable media.

Security validation may resolve symlinks internally if needed later, but configuration should preserve the original path string.

---

## 9. Sequential Ordering

### 9.1 Playlist mode

Sequential playlist playback follows the exact M3U entry order.

### 9.2 Folder mode

Sequential folder playback uses deterministic, case-insensitive **natural sorting** by relative path.

Natural sorting is required so that:

```text
1 - Intro.mp3
2 - Main.mp3
10 - Outro.mp3
```

plays as:

```text
1
2
10
```

rather than lexicographically as:

```text
1
10
2
```

For recursive folders, sorting should use the relative path so album/directory ordering remains deterministic:

```text
Album A/01 - Intro.flac
Album A/02 - Track.flac
Album B/01 - Track.flac
```

---

## 10. Random Ordering

Random mode is required for **both** folder and playlist sources.

Do not choose a new random array index independently before every track. Independent random selection with replacement causes repeats and starvation.

Instead use a shuffled queue / shuffle bag:

```text
source items
    |
    v
Fisher-Yates shuffle
    |
    v
play every item exactly once
    |
    v
rebuild / reshuffle next cycle
```

Properties:

- no item repeats within one complete shuffle cycle unless the source list itself contains duplicates;
- every queue entry receives one opportunity per cycle;
- playlist duplicates remain independent queue entries.

At a shuffle-cycle boundary, avoid making the first item of the new shuffled cycle equal to the final item of the previous cycle when more than one distinct candidate exists.

This prevents an obvious accidental:

```text
Track X
Track X
```

boundary repeat.

No shuffle order is persisted across source restarts.

---

## 11. Looping and Broadcast Semantics

There is no `loop` checkbox.

Local media is a broadcast source and is expected to continue indefinitely.

At the end of a playback cycle:

- rebuild/rescan the source;
- reconstruct sequential order or generate a fresh shuffle;
- continue playback.

This behavior also provides lightweight eventual refresh of changing folders and playlists.

---

## 12. Refresh and Rebuild Behavior

Avoid filesystem watchers in the initial implementation.

Rebuild the source item list:

1. when the source starts;
2. when relevant source configuration changes;
3. at the end of a complete playback cycle.

Consequences:

- newly copied files may not appear immediately;
- newly copied files naturally enter rotation at the next cycle;
- a user who wants immediate refresh may stop/start the relevant source/TX path;
- edited M3U files are re-read at the next rebuild.

Do not add inotify/watch services, debounce logic, or complex mutation tracking in v1.

---

## 13. Internal Local-Media Components

A clean conceptual separation is:

```text
FolderResolver
        |
        +--> list of MediaItems

M3uResolver
        |
        +--> list of MediaItems

        v

QueueBuilder
  - sequential
  - random/shuffle

        v

PlaybackQueue
  - next item
  - cycle completion

        v

GStreamer playback adapter
```

The resolver does not know about GStreamer state.

The queue builder does not scan the filesystem.

The GStreamer adapter does not parse M3U or decide shuffle semantics.

---

## 14. GStreamer Integration

### 14.1 Player contract

The playback side should conceptually request/receive the next `MediaItem`.

The queue/scheduler should not inspect GStreamer internals.

Conceptually:

```text
player needs next item
        |
        v
next()
        |
        v
MediaItem URI
```

### 14.2 Seamless track transitions

Track-to-track silence should be minimized.

The initial implementation should use GStreamer's next-item / `about-to-finish` style mechanism when compatible with the existing FMHeadend pipeline, so the next URI can be prepared before the current media item reaches EOS.

Desired behavior:

```text
Track A | Track B
```

rather than:

```text
Track A | silence | pipeline restart | Track B
```

The exact GStreamer implementation must be adapted to the existing player architecture instead of forcing a parallel playback stack.

### 14.3 EOS remains a fallback event

If preloading/next-URI handoff is not possible for a particular item, EOS may still trigger the next-item transition.

The queue abstraction should work with both pre-finish and EOS-driven transitions.

---

## 15. Local Playback Error Handling

A broadcast source must not stop because one media file is broken.

### 15.1 Per-item failure

Examples:

- file deleted after queue creation;
- permission failure;
- GStreamer decode failure;
- HTTP playlist entry unavailable;
- unsupported codec despite matching extension.

Behavior:

```text
log warning
skip failed item
request next item
continue broadcast
```

### 15.2 Empty source

If source resolution produces no playable candidates:

```text
NO_PLAYABLE_TRACKS
```

Examples:

- empty folder;
- no allowed audio extensions;
- empty playlist;
- playlist only contains unusable entries.

The source should enter a clear runtime error/idle state instead of spinning.

### 15.3 Entire cycle fails

A queue may initially contain candidates but every item may fail at playback time.

Example:

- 50 files were listed;
- storage disappeared;
- all 50 now fail to open.

Do not enter a tight infinite retry loop.

After a complete unsuccessful pass/cycle, transition to a clear error/idle state such as:

```text
PLAYBACK_EXHAUSTED
```

The concrete existing FMHeadend error representation should be reused where possible.

The important distinction is:

- `NO_PLAYABLE_TRACKS`: no usable candidates were resolved;
- playback exhausted: candidates existed, but an entire pass failed at runtime.

---

## 16. Local-Media Metadata — Near-Term Follow-Up

Metadata is not required for the first minimal local-playback implementation, but the architecture must not prevent it.

### 16.1 Desired metadata source

For local media:

1. prefer media/container tags;
2. use title/artist fields when available;
3. if no usable title exists, fall back to the filename without extension.

Example:

```text
/mnt/music/Boards of Canada - Roygbiv.flac
```

Fallback title:

```text
Boards of Canada - Roygbiv
```

Do not use M3U `#EXTINF` metadata as fallback.

### 16.2 Current item

The playback layer should eventually expose a current-item/current-track representation for:

- web UI "Now playing";
- diagnostics;
- later RDS integration.

### 16.3 Future RDS text

A likely music RT format is:

```text
Artist - Title
```

with sensible fallback when artist is unavailable.

RDS integration is not required in the first local-media Codex implementation unless explicitly requested in a later task.

---

## 17. Crossfade — Future Feature

True crossfade is explicitly outside the first local-media implementation.

Seamless next-item handoff and crossfade are different problems.

Seamless handoff:

```text
AAAAAAA|BBBBBBB
```

Crossfade:

```text
AAAAAAA
     BBBBBBB
     ^ overlap + gain ramps
```

Crossfade likely requires:

- two simultaneously active decode branches;
- a mixer;
- overlap timing;
- gain ramps;
- source-specific policy.

It is a future playback-engine feature, not basic playlist functionality.

Crossfade may be appropriate for music but undesirable for spoken-word podcast content, where overlapping the final sentence of one episode with the beginning of another would be harmful.

---

# Part II — Podcast / RSS Source

## 18. Podcast Source Goal

A future `podcast` source should allow FMHeadend to behave like an automated spoken-word/talk-radio scheduler.

One configured podcast source owns a **pool of RSS feeds**.

Each feed contributes eligible podcast episodes.

The source may then:

- randomly rotate episodes; or
- use a smarter radio-style scheduler that prioritizes freshness, unplayed content, cooldown rules, and feed diversity.

Conceptually:

```text
RSS feeds
    |
    v
EpisodeCatalog
    |
    v
Eligibility rules
    |
    +--------------------+
    |                    |
    v                    v
RANDOM bag         SMART scheduler
    |                    |
    +---------+----------+
              |
              v
         next MediaItem
              |
              v
       common player
```

The common playback engine should not need to know whether the item came from a folder, M3U, or RSS feed.

---

## 19. Podcast Source Configuration

Illustrative configuration:

```json
{
  "type": "podcast",
  "order": "smart",
  "refresh_interval_minutes": 30,
  "replay_cooldown_hours": 6,
  "feeds": [
    {
      "url": "https://example.com/news/feed.xml",
      "max_age_days": 3,
      "max_episodes": 20
    },
    {
      "url": "https://example.com/literature/feed.xml",
      "max_age_days": null,
      "max_episodes": null
    }
  ]
}
```

The exact schema can follow repository conventions.

### 19.1 Refresh interval

Podcast source refresh is source-level, not per-feed.

Default target:

```text
30 minutes
```

A refresh updates the known episode catalog but must not interrupt the currently playing episode.

---

## 20. Feed Eligibility Filters

Each feed has two independent optional filters:

```text
max_age_days
max_episodes
```

### 20.1 Both filters enabled

Both constraints apply.

An episode is eligible only if it is:

- not older than `max_age_days`; and
- inside the newest `max_episodes` entries for that feed.

### 20.2 One filter disabled

Only the enabled filter applies.

### 20.3 Both filters disabled

The full set of episodes currently exposed by the feed may remain eligible.

This is important for archival or evergreen content such as:

- literature;
- history;
- educational programmes;
- long-tail interview archives.

A three-year-old episode may be completely valid in such a source.

Eligibility filtering must not be confused with episode-history deletion.

---

## 21. Podcast Episode Catalog

RSS fetching/parsing creates an `EpisodeCatalog`.

Each episode should provide enough information to build a media item and maintain scheduler state.

Conceptual fields:

```text
episode identity
feed identity
enclosure/audio URI
episode title
podcast/feed title
publication timestamp
optional duration/other RSS metadata
```

The enclosure URL is the actual audio playback URI.

Do not require a filename extension in podcast audio URLs.

Valid podcast media may look like:

```text
https://cdn.example.com/audio?id=483934&token=...
```

GStreamer should determine actual media support.

---

## 22. Episode Identity and Light Duplicate Protection

Episode identity should primarily use RSS-native identity.

Preferred order:

1. feed identity + stable episode GUID;
2. feed identity + enclosure URL;
3. fallback identity derived from stable available fields if required.

Do not assume GUIDs are globally unique across unrelated feeds.

### 22.1 Cross-feed duplicate protection

A light duplicate guard is desirable because the same audio may occasionally appear in multiple related feeds due to:

- network/umbrella feeds;
- cross-posting;
- feed migration;
- syndication.

Do not over-engineer this.

An acceptable lightweight rule is:

- opportunistically suppress an exact duplicate enclosure URL within the same podcast source catalog.

Do **not** implement:

- audio fingerprinting;
- expensive content hashing;
- aggressive URL canonicalization;
- complex fuzzy-title deduplication.

If two semantically identical episodes use different enclosure URLs, duplication may remain. That is acceptable for this feature stage.

---

## 23. Podcast RANDOM Mode

Podcast RANDOM mode should behave like local random playback where practical:

```text
eligible episode set
        |
        v
shuffle bag
        |
        v
play each entry
```

Properties:

- no unnecessary replacement-style repeat;
- eligible episodes get a fair opportunity;
- refreshed catalog changes may be incorporated when rebuilding the remaining/next bag;
- current playback must not be interrupted by refresh.

RANDOM mode does not require the SMART scheduler's ranking logic.

---

## 24. Podcast SMART Mode

SMART mode is closer to a lightweight radio automation scheduler than a traditional playlist.

It should select the next episode **on demand** rather than constructing an hours-long immutable playlist.

Conceptually:

```text
EpisodeCatalog
      +
Eligibility
      +
Play history
      +
Previous selection context
      |
      v
choose_next()
      |
      v
MediaItem
```

This means a newly discovered episode can affect the very next scheduling decision after RSS refresh.

### 24.1 SMART scheduling goals

The scheduler should:

- strongly prefer episodes that have not been played;
- generally prefer fresher content;
- avoid unnecessary repetition;
- prefer a different feed from the immediately previous episode when alternatives exist;
- after all relevant episodes have been played, prefer less frequently / less recently played items;
- honor a replay cooldown when possible;
- never go silent merely because soft scheduling preferences cannot all be satisfied.

### 24.2 Do not use strict round-robin

Feed diversity is a soft scheduling preference, not a rigid sequence.

This is undesirable:

```text
Feed A
Feed B
Feed C
Feed A
Feed B
Feed C
```

as a mandatory invariant.

Instead:

> When multiple otherwise reasonable candidates exist, prefer an episode from a feed different from the previous episode.

If only one feed has viable content, the scheduler must continue using it.

### 24.3 Freshness versus never-played status

"Never played" is a strong preference but should not be hard-coded as an absolute rule that makes obviously stale archival content always outrank recent relevant programming.

The exact balance between:

- never-played status;
- publication freshness;
- play count;
- time since last play;

should remain implementation-tunable and covered by scheduler unit tests.

The design intent matters more than freezing arbitrary coefficients prematurely.

### 24.4 When everything has already played

Once all reasonable eligible candidates have history, favor:

- lower play count;
- older `last_played_at`;
- feed diversity where possible.

This naturally rotates the least-overused material back into the schedule.

---

## 25. Replay Cooldown

Podcast SMART mode supports a configurable replay cooldown with **hour-level granularity**.

Example:

```text
replay_cooldown_hours = 6
```

or:

```text
replay_cooldown_hours = 168
```

depending on content type.

This is intentionally expressed in hours because:

- current-affairs/news sources may reasonably repeat an episode after only a few hours;
- evergreen sources may want multi-day spacing.

### 25.1 Cooldown is soft

Cooldown must not be a hard condition that causes silence.

Selection logic:

1. prefer candidates outside cooldown;
2. if none exist, relax cooldown;
3. select the least-bad available candidate using normal history/freshness logic.

The source should prefer a repeat over dead air.

---

## 26. Podcast Play History

SMART scheduling requires persistent episode history.

This is intentionally different from local-file playback.

Suggested persistent information:

```text
episode_id
source_id
feed_id
play_count
last_played_at
last_seen_at
```

Additional fields may be introduced only if justified by the scheduler.

### 26.1 What is not persisted

Do not persist:

- current playback position;
- current queue;
- current random order;
- partially built scheduler state.

Restarting the source rebuilds the live catalog/selection state.

Only historical rotation information survives.

---

## 27. When an Episode Counts as Played

Do not mark an episode played immediately at item start.

Do not wait only for full EOS either.

Simple rule:

> After approximately 60 seconds of successful playback, record the episode as played.

At that point:

```text
play_count += 1
last_played_at = now
```

If playback fails before the threshold, do not count it as a successful play.

This rule is intentionally simple.

Do not add:

- completion percentages;
- duration-relative thresholds;
- resume positions;
- complex listening-progress semantics.

FMHeadend is scheduling a broadcast, not implementing a podcast client.

---

## 28. Feed Refresh Behavior

At each configured refresh interval:

```text
fetch feeds
    |
    v
parse successful responses
    |
    v
update EpisodeCatalog
    |
    v
apply eligibility
    |
    v
future choose_next() sees the new state
```

Do not interrupt the current episode.

### 28.1 Partial failures

If one feed fails but others work:

- log/report the failing feed;
- keep the source running;
- continue using valid catalog content.

### 28.2 Previously known feed fails to refresh

Do not immediately discard its last known catalog.

If a feed times out or returns bad XML:

```text
warning
keep previous known episode data
continue playback
```

Broadcast continuity is more important than aggressively treating temporary RSS failure as authoritative deletion.

### 28.3 Initial total failure

If no feed can provide any usable catalog and there is no cached/previous known content, the source may enter a runtime error state.

---

## 29. Podcast History Housekeeping

Do not delete play history simply because an episode becomes ineligible under current policy.

Example:

```text
max_age_days = 7
```

causes an eight-day-old episode to leave the candidate pool.

That does **not** mean its history should be deleted.

If the user later disables the age limit, the episode should not incorrectly reappear as never played.

### 29.1 `last_seen_at`

A successful feed refresh should update `last_seen_at` for episodes that still exist in the feed, even when they are currently excluded by `max_age_days` or `max_episodes`.

This separates:

```text
known by feed
```

from:

```text
currently eligible for scheduling
```

### 29.2 Cleanup policy

History cleanup should be conservative.

Episodes may disappear from an RSS document because publishers cap feed length even though the media still exists.

Therefore:

- do not delete immediately when an episode disappears;
- retain history generously;
- later implementation may prune entries that have not been seen for a long time;
- housekeeping should not be a near-term scheduler priority.

A bounded long-retention policy is preferable to aggressive deletion.

---

## 30. Podcast Metadata and RDS

Podcast RSS already supplies meaningful source-native metadata.

Desired fields:

```text
podcast/feed title
episode title
```

Desired future RDS RadioText format:

```text
Podcast title - Episode title
```

Example:

```text
Example Politics Show - Election Night Special
```

Do not force podcast titles into a music-specific `artist` semantic merely to reuse a string slot.

The internal metadata model should be sufficiently general to distinguish:

- track title;
- artist;
- collection/show/podcast title.

A later RT formatter may choose presentation based on source type.

---

## 31. Future Feed Weighting

A future scheduler may support feed weights, for example:

```text
Feed A weight = 1
Feed B weight = 2
```

This could intentionally give some feeds more airtime.

This is explicitly **not near-term scope**.

The SMART scheduler should first work well using:

- freshness;
- unseen preference;
- cooldown;
- play history;
- feed diversity.

Do not introduce weighting complexity until a real use case requires it.

---

# Part III — Shared Playback Abstractions

## 32. Common Player Interface

Local files, remote M3U entries, and podcast enclosures should converge at one playback abstraction.

Conceptually:

```text
next item provider
        |
        v
MediaItem { uri, metadata... }
        |
        v
GStreamer adapter
        |
        v
existing FMHeadend PCM/audio pipeline
```

Potential next-item providers include:

```text
PlaybackQueue           // local sequential/random
PodcastRandomBag        // podcast random
PodcastScheduler        // podcast smart
```

The player does not need to know which provider produced the item.

---

## 33. Event Flow

A clean event relationship is:

```text
GStreamer playing item
        |
        +--> about-to-finish
        |       |
        |       v
        |   request next MediaItem
        |
        +--> EOS fallback
        |       |
        |       v
        |   request next MediaItem
        |
        +--> playback/decode error
                |
                v
            report failure
            request next item
```

The queue/scheduler reacts to abstract playback events.

It should not query GStreamer state directly to decide source ordering.

---

## 34. Error Isolation

A failed media item should generally affect the item, not the entire transmitter chain.

The common principle is:

```text
bad item -> warn -> skip -> next
```

Only broader conditions such as:

- no candidates;
- every candidate fails;
- no podcast catalog available;

should place the source into an error/idle state.

This is especially important for unattended broadcast-style operation.

---

# Part IV — Implementation Scope and Sequencing

## 35. First Implementation Target

The first Codex implementation should focus on the local-media source only.

Required initial functional scope:

- local source family;
- `playlist` mode;
- `folder` mode;
- absolute host path configuration;
- folder recursive flag;
- sequential order;
- random order;
- shuffle-bag semantics;
- M3U/M3U8 parsing;
- relative local M3U paths;
- absolute local M3U paths;
- HTTP/HTTPS M3U entries;
- M3U duplicate preservation;
- folder extension filtering;
- natural relative-path sorting;
- hidden-file/directory ignore;
- safe symlink-directory behavior;
- transient queue lifecycle;
- rebuild on source start;
- rebuild at cycle boundary;
- existing shared-source/fan-out semantics;
- skip failed tracks;
- protection against an all-items-failing retry loop;
- GStreamer next-item integration;
- `about-to-finish`-style seamless transition where compatible;
- simple web UI configuration using a path textbox;
- no required save-time path existence check.

The implementation should reuse existing FMHeadend source, lifecycle, audio, fan-out, status, and error conventions.

Do not build a parallel media subsystem if the existing architecture already provides the required hooks.

---

## 36. Explicit Non-Goals for the First Local-Media Implementation

Unless separately requested, the first implementation should **not** include:

- frontend filesystem browser;
- automatic mount management;
- filesystem watchers/inotify;
- persisted local playback position;
- persisted local shuffle state;
- crossfade;
- podcast/RSS source;
- podcast scheduler/history;
- feed weighting;
- RDS integration;
- full now-playing UI;
- M3U `#EXTINF` metadata processing;
- media-library database;
- heavy pre-probing of every folder file.

These may be implemented in later, deliberately scoped changes.

---

## 37. Near-Term Follow-Ups

After basic local playback is proven stable, likely follow-up work includes:

1. Current-track metadata:
   - parse media tags;
   - title/artist;
   - filename-without-extension fallback.

2. Web UI now-playing/status.

3. RDS RT integration:
   - music: `Artist - Title`;
   - podcast later: `Podcast title - Episode title`.

4. Podcast/RSS source:
   - multi-feed pool;
   - filters;
   - refresh;
   - RANDOM;
   - persisted history;
   - SMART scheduler.

5. Crossfade as a separate playback-engine project.

---

## 38. Design Rules for Future Coding Tasks

Coding agents working from this document should follow these rules:

- Read the existing source/playback architecture before adding new abstractions.
- Preserve existing shared-source semantics.
- Prefer reuse of the existing GStreamer/audio path.
- Do not let playlist/folder/podcast logic leak into GStreamer internals.
- Do not let GStreamer-specific URI/state behavior leak into source configuration.
- Keep local configuration path-based and server-side.
- Keep item selection testable without real GStreamer playback.
- Keep podcast scheduler logic testable with synthetic episode/history inputs.
- Treat broadcast continuity as a primary concern.
- Prefer simple, deterministic behavior over feature-heavy heuristics.
- Do not implement future-scope features merely because they are described here; each coding task will explicitly define its implementation scope.

---

## 39. Summary of Key Decisions

### Local media

- One logical local-media source with `playlist` and `folder` modes.
- Absolute host filesystem paths remain the long-term API/config representation.
- Initial UI uses a plain path textbox.
- NAS and USB work through normal OS mountpoints.
- Playlist and folder modes both support sequential and random playback.
- Random uses a shuffle bag, not independent random indexes.
- M3U duplicate entries are preserved.
- M3U may contain local paths and HTTP/HTTPS media URLs.
- `#EXTINF` metadata is ignored.
- Folder sequential order is case-insensitive natural relative-path order.
- Recursive scan ignores hidden paths and does not follow symlinked directories.
- Source restart rebuilds everything; no local playback state is persisted.
- End-of-cycle rebuild provides simple eventual content refresh.
- One bad track is skipped.
- A fully failing cycle must not spin forever.
- GStreamer `about-to-finish` / next-item handoff should be used to minimize gaps.
- Crossfade is future scope.

### Podcast

- Podcast is a separate future source type built on the same MediaItem/player concepts.
- One source contains multiple RSS feeds.
- `max_age_days` and `max_episodes` are independent optional per-feed filters.
- Both filters may be disabled.
- Source refresh default target is 30 minutes.
- RANDOM uses shuffle-style semantics.
- SMART chooses the next episode on demand.
- SMART prefers unseen/fresh material, feed diversity, low play counts, and long time-since-play.
- Replay cooldown is configurable in hours and is a soft constraint.
- SMART requires persistent per-episode play history.
- An episode counts as played after roughly 60 seconds of successful playback.
- History survives policy changes; eligibility is not history deletion.
- Exact duplicate enclosure URLs may receive lightweight cross-feed suppression.
- No heavy deduplication/fingerprinting is required.
- Future podcast RT format is `Podcast title - Episode title`.

---

This document describes the intended architecture and behavior. Individual implementation tasks should deliberately select a subset of this design and must not silently expand scope beyond the task being implemented.
