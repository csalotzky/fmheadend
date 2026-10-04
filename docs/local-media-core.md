# Local media core (resolver and queue only)

Implementation: `daemon/sources/local_media.py`, within the existing sources package.
Design: [local media and podcast source design](fmheadend_local_media_podcast_design.md).
No source type, API, schema, database or playback integration is registered yet.

## Internal contract

`LocalMediaConfig(mode, path, recursive=False, order=SEQUENTIAL)` uses the existing
frozen-dataclass/StrEnum conventions. Paths must be absolute host filesystem paths;
construction does not check existence. The configured symlink spelling is preserved.
`resolve_media(config)` dispatches to `FolderResolver` or `M3uResolver`.

`MediaItem.locator` is either an absolute `pathlib.Path` or an HTTP(S) URL string.
No `file://` conversion occurs here. No metadata fields are needed yet.
Local playlist entries are relative to the playlist's containing directory; they
are not opened, deduplicated or extension-filtered. Local path spelling (including
symlinks) and remote URL spelling are retained. Boundary equality uses these
locators, not inode identity, redirect resolution or media fingerprinting.

Folder discovery ignores dot entries, never descends through directory symlinks,
and accepts symlinks resolving to regular candidate files. The configured root may
itself be a directory symlink. Candidate files are not decoded or opened; permission
and decoder failures during playback remain the later player's responsibility.
An unreadable traversed directory fails the scan rather than silently returning
an incomplete candidate list. Natural sorting compares case-folded relative path
components and numeric runs; original relative spelling breaks otherwise-equal ties.

Candidate extensions: `.mp3`, `.flac`, `.wav`, `.ogg`, `.opus`, `.m4a`, `.aac`.
Read-only target inspection on 2026-10-04 found GStreamer 1.26.2 with
`mpg123audiodec`, `flacdec`, `wavparse`, `oggdemux`, `vorbisdec`, `opusdec`,
`qtdemux`, `aacparse`, and `avdec_aac`. This supports conservative candidate
selection, not a promise that every matching file can be decoded.

Both M3U and M3U8 are read as strict UTF-8 with optional BOM. Blank lines and
comments/directives (after surrounding whitespace is stripped) are ignored.
No legacy encoding detection or extended-playlist metadata handling is added.
Unsupported URI schemes or malformed HTTP(S) entries are rejected explicitly.
Filesystem paths follow the host OS's path syntax, not foreign-platform drive rules.

Errors reuse `DomainError`: `FOLDER_UNREADABLE` / `PLAYLIST_UNREADABLE` (503),
`INVALID_PLAYLIST_ENTRY` (422), and `NO_PLAYABLE_TRACKS` (409). These are internal
resolver results, not newly exposed API endpoints. Runtime playback errors are
not implemented.

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

Future integration owns shared-source playback, rescan timing, URI conversion,
decoder failures, and continuity. Metadata, RDS, RSS/podcasts and frontend work are
all deferred. No actual media playback or RF acceptance is claimed by these tests.
