# Local media core, configuration and runtime

Implementation: `daemon/sources/local_media.py`, within the existing sources package.
Design: [local media and podcast source design](fmheadend_local_media_podcast_design.md).
The existing source model/API persists `LOCAL` configuration. `GstPipeline` now
plays it through the existing shared PCM output. Configuration validation does not
resolve media; starting/rebuilding an active source does.

## Internal contract

`LocalMediaConfig(mode, path, recursive=False, order=SEQUENTIAL)` uses the existing
frozen-dataclass/StrEnum conventions. Paths must be absolute host filesystem paths;
construction does not check existence. The configured symlink spelling is preserved.
`resolve_media(config)` dispatches to `FolderResolver` or `M3uResolver`.

`MediaItem.locator` is either an absolute `pathlib.Path` or an HTTP(S) URL string.
No `file://` conversion occurs here. No metadata fields are needed yet.
Local playlist entries are relative to the playlist's containing directory; they
are not opened, deduplicated or extension-filtered. Local path spelling (including
symlinks) is retained after one UTF-8 percent-decoding pass; remote URL spelling
is retained unchanged. Boundary equality uses these
locators, not inode identity, redirect resolution or media fingerprinting.

Folder discovery ignores dot entries, never descends through directory symlinks,
and accepts symlinks resolving to regular candidate files. The configured root may
itself be a directory symlink. Candidate files are not decoded or opened; permission
and decoder failures during playback are the runtime player's responsibility.
An unreadable traversed directory fails the scan rather than silently returning
an incomplete candidate list. Natural sorting compares case-folded relative path
components and numeric runs; original relative spelling breaks otherwise-equal ties.

Candidate extensions: `.mp3`, `.flac`, `.wav`, `.ogg`, `.opus`, `.m4a`, `.aac`.
Read-only target inspection on 2026-10-04 found GStreamer 1.26.2 with
`mpg123audiodec`, `flacdec`, `wavparse`, `oggdemux`, `vorbisdec`, `opusdec`,
`qtdemux`, `aacparse`, and `avdec_aac`. This supports conservative candidate
selection, not a promise that every matching file can be decoded.

Both formats accept an optional UTF-8 BOM and ignore blank lines/directives.
Legacy `.m3u` comment lines are discarded as bytes before UTF-8 decoding, so VLC
`#EXTINF` text in a legacy encoding cannot invalidate usable entries. Actual entry
lines still require UTF-8; no charset guessing is performed. `.m3u8` remains strict
UTF-8, including its comments. A malformed concatenated line beginning with `#`
is ignored as a whole; no metadata extraction or heuristic recovery is attempted.

Local relative/absolute entries are percent-decoded once using UTF-8 before Path
construction and relative resolution. `+` stays `+`; `%2520` becomes literal `%20`,
not a space. To represent a literal percent escape in a filename, encode its `%`
as `%25`. HTTP(S) entries are classified first and preserved verbatim, including
encoded paths/query strings. Invalid UTF-8 in percent escapes or decoded NULs
produce `INVALID_PLAYLIST_ENTRY`; missing/unreadable playlist files retain their existing
`PLAYLIST_UNREADABLE` error. No save-time availability checks are added.
Unsupported URI schemes or malformed HTTP(S) entries are rejected explicitly.
Filesystem paths follow the host OS's path syntax, not foreign-platform drive rules.

Errors reuse `DomainError`: `FOLDER_UNREADABLE` / `PLAYLIST_UNREADABLE` (503),
`INVALID_PLAYLIST_ENTRY` (422), and `NO_PLAYABLE_TRACKS` (409). These are internal
resolver results, not newly exposed API endpoints. Runtime reports their safe
error codes through the existing source ERROR/last_error fields.

## Cycle ownership

```python
items = resolve_media(config)
queue = PlaybackQueue(items, config.order)
item = queue.next_item()  # MediaItem, or None when the cycle is exhausted
# Later, the source owner rescan boundary will explicitly do:
queue.rebuild(resolve_media(config))
```

`PlaybackQueue` owns a copy of the candidates and one finite cycle. It returns
`None` at exhaustion until the owner calls `rebuild`; it never silently rescans or
loops an obsolete list. `rebuild()` without arguments reuses the candidate list,
while `rebuild(new_items)` replaces it. A fresh instance resets all history.
An empty candidate list yields an exhausted queue immediately, without spinning.
Resolvers distinguish this case by raising `NO_PLAYABLE_TRACKS`.

`QueueBuilder` copies the list and uses standard-library `Random.shuffle` for random
cycles. Duplicate entries keep their multiplicity. If the new first locator equals
the last consumed locator, a uniformly selected different entry is swapped into
first position when possible. One-item/all-identical bags cannot avoid that repeat.
The RNG can be injected for deterministic tests. No caller list is mutated.
The queue is single-owner state; there are no worker threads, callbacks or locks.

Runtime ownership is described below. LOCAL and HTTP share transient source
metadata and the existing RadioText route. RSS/podcasts and frontend Now Playing
remain deferred; native synthetic-media tests do not constitute RF acceptance.


## Persisted source configuration

Use the existing `/api/v1/sources` CRUD endpoints and uppercase source-type convention:

```json
{
  "name": "Night rotation",
  "type": "LOCAL",
  "config": {
    "mode": "folder",
    "path": "/mnt/nas/music",
    "order": "random",
    "recursive": true
  }
}
```

`mode` and `path` are required. Modes are `folder` / `playlist`; order is
`sequential` (default) / `random`. `recursive` is a strict boolean, default false;
playlist mode stores it unchanged but ignores it. Existing source-level gain,
profile selection, IDs and shared modulator references are unchanged.

`LocalConfig` is the normal Pydantic type-specific configuration validator. It
reuses the core enums; `to_core()` is the explicit conversion to `LocalMediaConfig`
on the playback host. Conversion is not part of persistence. The API
accepts absolute Linux target paths even when its tests run on Windows, plus native
absolute host paths. Core path operations remain native to the playback host.
The saved path string is never canonicalized, resolved, trimmed or converted to a
URI. Missing/unreadable mounts and symlink targets are not checked. Empty/relative
paths, NULs, URIs, unsupported enum values and unexpected fields are rejected.

PATCH follows existing source semantics: `config` replaces the entire sub-object,
so supply mode/path when editing it; missing optional fields take their defaults.
Mode/type changes remain supported through the ordinary source update operation.
The existing SQLite JSON source row holds configuration only; no table change,
version bump or migration is required (schema stays 1).

## Runtime ownership and transitions

`SourceManager` uses its ordinary shared `GstPipeline` factory for LOCAL. Attaching
another TX adds a consumer to the same `PcmOutput`; it does not create a player or
queue. Detaching the last consumer stops the worker and discards all transient
selection state. A true restart resolves again and creates a fresh shuffle/sequence.

`local_playback.py` owns `PlaybackQueue`, a prepared next URI and bounded handoff
bookkeeping. It delegates ordering/duplicates/boundary-repeat avoidance to the
existing core. Filesystem paths become escaped `Path.as_uri()` values only here;
HTTP(S) playlist entries go directly to GStreamer. Nothing downloads media itself.

LOCAL uses native **playbin3** with an audio-only custom sink bin containing the
same convert/resample/source-gain/DSP/final-caps/queue/appsink chain used by other
sources. Appsink still feeds bounded staging, the source worker, and `PcmOutput`.
HTTP/HLS/UDP/RTP/ALSA construction is unchanged. No per-TX playback or second
Python audio path is introduced. The target needs the standard `playbin3` element.

The native `about-to-finish` callback consumes only an already-prepared URI. Queue
advancement/resolution runs on the existing source worker, never in that streaming
callback. Once the current queue has been submitted, the worker re-resolves for
the next cycle (prefetch can occur before the last item finishes audibly).
At least one item must have produced valid decoded PCM before a cycle is rebuilt.
STREAM_START at appsink associates PCM with the correct cycle despite prefetch.

If no URI was ready for handoff, EOS advances through a fallback: put the same
native player in NULL, assign the next URI and return to PLAYING. Error/stall
recovery uses this same path. Existing native elements, DSP configuration,
PcmOutput and consumers survive; decoded tail PCM retains normal staging age
semantics. Exceptional resets can interrupt audio/reset DSP history. Normal native
handoff does not tear down the downstream chain. URI assignment and teardown are
serialized; a streaming callback never waits for the teardown lock.

Resolution errors (including `NO_PLAYABLE_TRACKS`) are visible terminal source
errors until an explicit source restart. Failed/unreadable/undecodable/stalled
items are skipped. A complete cycle with no actual PCM ends in
`PLAYBACK_EXHAUSTED`, without a tight retry loop. One successfully decoded item
permits another cycle. Logs report failure categories, not remote credentials or
query strings. No runtime queue/track state is persisted.

The existing source-test contract is reused: an active probe observes the shared
pipeline without advancing it; an inactive probe runs an independent temporary
pipeline until decoded PCM or its existing timeout, then stops. It does not expose
a media-library API or change the active selection.

### Native validation and limitations

Isolated Raspberry Pi/GStreamer 1.26.2 tests generate synthetic WAV tones and use
appsink/fanout only (no mux, SPI or RF). They cover sequential/random folders,
M3U local and localhost HTTP entries, native handoff, forced EOS fallback, bad and
empty media, shared consumers, stop/restart, cycle refresh and source/profile gain.
Normal handoff retains the same downstream objects. This is near-seamless playback,
not a sample-perfect gapless guarantee: exceptional EOS recovery has measurable
silence, and filesystem scans on a slow/offline mount can delay the source worker.
A blocked OS filesystem call is not forcibly cancellable; existing stop timeout
and ownership protection apply. No filesystem watcher or extra scanning worker
is introduced.

A direct `uridecodebin3` experiment reset timestamps between items and delivered
four 0.5-second files in about 0.55 seconds despite a synchronous sink. Native
playbin3 segment management correctly paced the same content (about 2.33 seconds
including startup). This is why LOCAL wraps the existing audio chain as a playbin3
sink rather than assigning successive URIs to the existing HTTP uridecodebin.


## Transient source metadata

HTTP/ICY bus tags and LOCAL serialized appsink TAG events use the same tag parser
and `Artist - Title` formatter in `GstPipeline`. Both publish the existing
`title`, `artist`, `stream_title`, codec and bitrate fields. SourceManager,
InputSourceStatus, status publication and RadioTextService require no new fields
or alternate routing. ICY station identity (`icy-name` / `station_name`) remains
separate and unchanged.

LOCAL's appsink STREAM_START associates the rendering item with its queued URI.
A bounded pending metadata dictionary clears the old artist/title and initializes
title from the decoded filesystem filename stem. Remote entries use the decoded
URL path stem only, never credentials, query or fragment; playback URIs are not
changed. Serialized TAG events merge usable title/artist values for that stream.
The dictionary is published atomically to the generic source snapshot at the next
valid PCM callback. No filesystem or network work occurs in metadata callbacks.
This keeps preroll and about-to-finish preparation from announcing the next item
early. Late/unassociated bus TAG messages are ignored for LOCAL: only serialized
events at the rendering sink can change its pending metadata.

Tagged title takes precedence over filename fallback. Artist is never inferred
from filenames; missing artist remains null. Empty title tags retain the fallback.
A new stream resets item metadata, preventing the previous artist from leaking
into an untagged item. EOS/error recovery clears old metadata while playback is
unavailable; normal handoff retains it until the next stream produces PCM. Final
stop discards it, and restart constructs it afresh. Probes reuse the same snapshot
without waiting longer solely for tags. `#EXTINF` remains ignored.

The existing RadioText selector continues to choose runtime override, then shared
source `stream_title` when the modulator enables metadata-to-RT, then persistent
fallback. Each TX applies this unchanged selection independently. No metadata is
stored in configuration/SQLite and no RDS encoding, scheduling or transport changes
are involved. The published text is source-selected metadata, not receiver-confirmed
on-air content; existing PCM/RDS buffering may delay physical presentation.
