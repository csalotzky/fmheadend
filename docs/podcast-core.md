# Podcast source core and shared playback runtime

`PODCAST` is a normal shared InputSource configuration, using the existing source
CRUD and saved/unsaved test endpoints. Active sources now play through the shared
sequenced media runtime described below; feed tests remain catalog-only.

```json
{
  "name": "Talk rotation",
  "type": "PODCAST",
  "config": {
    "feeds": [
      {"url": "https://example.org/feed.xml", "max_age_days": 30, "max_episodes": 10},
      {"url": "https://example.net/feed.xml", "max_age_days": null, "max_episodes": 5}
    ],
    "schedule_mode": "SMART",
    "refresh_interval_minutes": 30,
    "replay_cooldown_hours": 24
  }
}
```

Defaults are SMART, 30 minutes, 24 hours, and null feed limits. Age/count limits
are non-negative integers (zero really limits to zero age/count, not unlimited).
Refresh is a positive integer; cooldown is a non-negative integer. Up to 32 unique
HTTP(S) feed URLs, each at most 8192 characters, bound probe work and diagnostics.
Embedded URL credentials, unsupported schemes and malformed URLs are rejected;
private/LAN HTTP servers remain allowed, consistent with this trusted appliance's
existing source URLs. Saving never fetches or checks reachability. PATCH replaces
the whole config sub-object, as for existing source types.

## Components and ownership

- `podcast.py`: immutable feed/episode/catalog snapshots, parsing, eligibility.
- `podcast_fetch.py`: bounded HTTP fetching and isolated source-test probe.
- `podcast_history.py`: SQLite history repository on the Database owner thread.
- `podcast_schedule.py`: single-owner transient RANDOM/SMART selection.
- `podcast_runtime.py`: active source policy, periodic refresh and PCM history.
- `sequenced_playback.py`: shared LOCAL/Podcast prepared/submitted/rendering tickets.

`feedparser>=6.0.14,<7` is the maintained RSS/Atom parser; its
[reference documentation](https://feedparser.readthedocs.io/en/latest/) describes
enclosures, entry IDs, parsed dates and format/encoding handling. It receives bytes,
never a URL to fetch. `httpx` (already used by backend tests, now a runtime dependency)
handles HTTP. No custom XML parser or audio HTTP client was added.

`FeedFetcher()` is scoped to **one refresh batch**: construct a fresh instance for
each refresh. It has a 20-second batch deadline checked between requests/chunks,
five-second HTTP operation timeouts capped by remaining budget, at most five
redirects per feed, and 4 MiB limits on both wire and decompressed bodies. A final
in-flight network operation can finish after the deadline; socket/DNS timing is
subject to the platform resolver. There are no retries. All redirect targets must
remain HTTP(S) without embedded credentials. Identity encoding is requested; gzip
is supported with bounded decompression, other encodings rejected. The User-Agent
identifies FMHeadend. Feed warnings/probe errors contain codes and feed hashes, not
URLs, query tokens or exception bodies. TLS verification remains enabled.

The core exposes `catalog.refresh(config, FeedFetcher(), aware_now, history=...)`
and `refresh_due`. The active runtime schedules refresh by monotonic time rather
than wall-clock changes, as described below. Refresh serializes writers,
builds a complete immutable snapshot outside the publication lock, then replaces it
atomically. Scheduling can read the prior snapshot during network I/O. Successful
feeds replace their prior content (including a legitimately empty feed); failed feeds
retain their prior content. Removed configured feeds disappear from the new snapshot.
Failed attempts also advance the refresh deadline, preventing tight retry loops.

## Identity, normalization and eligibility

Feed identity hashes the exact configured URL, not its redirect destination.
Episode identity hashes a GUID/stable Atom ID, or the enclosure URL when no ID
exists; the key is `(feed_id, episode_id)`. Every accepted episode has an enclosure,
so no mutable-title fallback is necessary. Duplicate IDs collapse within a feed;
publication time, then URL/title deterministically break conflicting duplicate ties.
Distinct GUIDs and feed namespaces remain distinct, even if their enclosure matches:
optional cross-feed audio deduplication is deliberately not implemented.

Enclosures must resolve to HTTP(S). Audio MIME types are preferred, followed by
untyped/application-octet-stream enclosures; lexical URL order breaks equal ranks.
Known video/image links are excluded. No extension or audio download is required.
Relative links use the fetched feed location. Titles become bounded plain text with
HTML entities decoded and markup removed. Descriptions are not retained. Episode
objects include show/title, URL, publication, first discovery and last-seen times.
`episode.media_item()` returns the existing neutral `MediaItem` without modifying
LOCAL or introducing a second playback representation.

Valid publication dates become UTC-aware datetimes. Missing/malformed dates remain
unknown, never 'now'. With an age limit, unknown dates are conservatively excluded.
Without an age limit they remain usable, sorted after dated entries by stable key.
Age includes the exact boundary. Apply age first, then retain each feed's newest
`max_episodes`. Both limits apply. Feed XML ordering does not determine undated or
equal-date order. Empty eligibility is distinct from feed fetch failure; scheduling
raises `NO_PLAYABLE_EPISODES` and an empty configured pool `NO_PODCAST_FEEDS`.

## Scheduling

The caller passes current eligible episodes and a detached history snapshot to
`choose_next(episodes, history, aware_now, replay_cooldown_hours=24)`. No filesystem,
network, SQL or GStreamer work occurs in selection. The returned episode includes
its identity for future history updates and can produce a MediaItem for the player.

**RANDOM:** standard-library shuffle over unique episode keys. Each remaining bag
entry is consumed once. Removed keys are pruned before every selection, updated
episode objects/URLs are used immediately, and additions enter the next bag.
Rollover avoids last/first repetition when at least two keys exist. RNG injection
makes tests deterministic. No bag state is persisted.

**SMART**, in this exact order:

1. Prefer candidates outside cooldown (last play at least the configured duration
   ago, including the exact boundary). If none exist, use all eligible candidates.
2. If any candidate in that cooldown-filtered pool is unplayed (`play_count=0`),
   choose only among all its unplayed episodes, across all feeds. Rank by newest
   known publication timestamp, different feed from the preceding selection, then
   stable identity. Known dates rank ahead of unknown dates; lacking a date never
   excludes an episode, and an undated unplayed episode beats any played candidate
   remaining after cooldown.
3. If all candidates have played, rank by lower play count, older last-play time,
   different previous feed, newest publication timestamp, stable key.

`max_age_days` controls age eligibility; SMART introduces no hidden age cutoff.
Freshness is only a ranking preference. Undated-only pools remain deterministic
through diversity and stable identity, without invented dates. Cooldown runs before
the global unplayed preference and relaxes only when every candidate is inside it.

Only the preceding selection is remembered. This policy is deterministic, not a
statistical score; new unplayed episodes can affect the next choice immediately.
Cooldown/freshness/diversity never make a nonempty eligible pool unselectable.
Selection alone does not mark an episode played. The shared playback runtime handles
failures and successful-PCM accounting; repeatedly selecting without reporting
playback success is not a simulation of a completed broadcast rotation.

## Durable history and schema 1

The schema-1 creation path adds `podcast_history` with composite primary key
`(source_id, feed_id, episode_id)`, `play_count`, nullable `last_played_at`, and
`last_seen_at`. Source deletion cascades its history. Merely removing feeds/episodes,
changing filters/type, or aging out does **not** delete it. No cleanup policy is added.
Successful refresh observes all discovered episodes, including ineligible ones;
failed refresh does not advance their last-seen timestamps.

`PodcastHistory(database, source_id).mark_played(episode, aware_played_at)` increments
the count and updates play time transactionally. Runtime invokes it once per
occurrence after 60 seconds of actual decoded PCM, never on selection/probing.
Repository operations stay on the owning SQLite thread; schedulers receive detached
history snapshots. No history schema or migration change accompanies playback.

DB and JSON versions remain **1**. No migration or compatibility path is added.
As with earlier pre-release schema changes, old physical database structures fail
startup validation unchanged; deployment must deliberately recreate/import development
configuration rather than expecting this code to upgrade an old database.

## Source test and deferred work

Saved and unsaved source tests create an isolated catalog and return bounded
`podcast` diagnostics: attempted/successful feed counts, per-feed hash/error/discovered
counts, total discovered and eligible episodes. `ok` means at least one eligible
enclosure; partial failures are reported even when `ok=true`. It does not promise
audio decoding. Tests do not persist history/catalog, start GStreamer, or touch the
shared runtime. Normal `/status` still reads cheap published observations and does
not fetch RSS or expose the episode catalog.

Episode browsing, Next/Skip, audio caching, seeking/resume and crossfade remain
deferred. Existing source configuration UI and RDS implementation are unchanged.

## Active runtime and recovery

SourceManager constructs one GstPipeline per shared source, exactly as for LOCAL.
The existing playbin3/custom audio sink, normalization, staging, DSP and PcmOutput
fanout are reused. `sequenced_playback.py` contains the extracted LOCAL URI handoff
bookkeeping; LOCAL retains its resolver/cycle policy. Podcast supplies catalog and
scheduler policy to that same player. Enclosure MediaItems retain their exact URI.

Each active Podcast runtime has one I/O thread and one SQLite connection opened
and closed on that thread against the persisted source database. It loads durable
history, performs initial refresh, then refreshes at `refresh_interval_minutes`
after the preceding attempt finishes, using monotonic timing. Streaming callbacks
never fetch RSS or access SQLite. No TX owns an independent scheduler or counter.
Source tests still perform the existing isolated catalog probe, even during playback.

The I/O thread publishes an immutable catalog view and clears suppression only for
feeds that refreshed successfully. The source worker computes eligibility and calls
the unchanged scheduler under the short ticket lock, against one catalog/history/
suppression view. The lock is never held during network or database I/O. History
threshold changes update the detached scheduling view immediately and queue the
durable write. A refresh does not interrupt the current native item.

No catalog/eligible item, initial RSS failure, or playback exhaustion leaves the
player idle in ERROR with empty programme metadata. The source and I/O worker stay
owned while consumers remain. They do not retry failed playback continuously.
Scheduled refresh wakes the source worker; a retryable candidate automatically
restarts the same native player/output. A failed refresh retains known catalog data
and failure suppression. A successful feed refresh permits its failed episodes to
retry once in that refresh opportunity. There is no frontend-triggered timer.

Prepared tickets are not current episodes. Native about-to-finish only submits the
already-prepared URI. Serialized appsink STREAM_START associates the next ticket;
first valid normalized PCM makes it active and publishes its metadata. Known old
native source errors are ignored using a bounded source/occurrence association.
Bus TAG messages are ignored for sequenced media; serialized sink events cannot
overtake old PCM. Exceptional NULL recovery quiesces callbacks and flushes the bus.
Unassociated decoder errors cannot safely identify a particular prefetched item:
recovery conservatively suppresses all interrupted rendering/submitted occurrences.

Refresh invalidates an unsubmitted ticket that is no longer eligible. SMART also
reconsiders unsubmitted preparation after catalog/history changes; an already native-
submitted ticket may complete. RANDOM retains its selected bag item while eligible;
no alternate random chooser is added. EOS fallback resets the same player when no
native handoff was ready. Normal handoff retains the downstream chain.

## Metadata, successful PCM and failures

RSS `feed_title` maps to generic `artist`, episode title to generic `title`, and the
existing formatter produces `Podcast - Episode` (or just episode without a feed
title). Publication occurs at actual PCM, never URI assignment/preroll. Embedded
title/artist tags cannot replace RSS identity; codec/bitrate tags still apply.
The ordinary InputSourceStatus and RadioText selector are reused unchanged, including
override > enabled source metadata > persistent fallback. No Podcast status/RT API
or new metadata event path exists. HTTP/ICY and LOCAL metadata behavior is unchanged.

Each ticket represents one playback occurrence. Valid, nonempty, frame-aligned
48 kHz stereo S16LE appsink blocks add their byte length before staging/fanout.
`60 * PCM_BYTES_PER_SECOND` is exactly **11,520,000 bytes**, or **2,880,000 stereo
frames**. This measures successful decoding, not RF output or consumer delivery;
downstream queue drops do not subtract already decoded audio. Buffering/wall time
without PCM contributes nothing. At threshold the lock sets a once-only ticket flag
and enqueues one history update. Later blocks or multiple TX consumers cannot count
it twice. A future occurrence uses a new ticket. A bounded 64-event history queue
reports an explicit error if I/O cannot keep up; DB errors are not blindly replayed
because the commit outcome could be uncertain.

Sub-60-second occurrences are **never counted**, even on EOS/native handoff. The
shared path does not prove known per-item duration plus clean completion closely
enough to make that safe. No percentage or wall-clock approximation is used. Such
short episodes remain unplayed in SMART history and may be chosen again by its
unchanged policy. Failure after any PCM amount suppresses immediate reselection;
failure before threshold creates no fake history, and failure after threshold never
undoes a legitimate increment. Suppression is transient and separate from eligibility.

Final-consumer stop first quiesces native playback, closes tickets and cancels refresh,
then joins the I/O owner and flushes already-earned history events. An in-flight HTTP
operation must finish/timeout; further feed requests are cancelled and late publication
cannot revive closed state. Stop retains ownership and reports an error if cleanup
exceeds its deadline, rather than starting a competing owner. OS DNS/network calls
remain subject to the bounded-transport/platform limitation described above.
Successful cleanup discards catalog, active/prepared occurrences, counters and failed
keys, and closes the SQLite connection. Config edits use existing SourceManager
restart semantics: fresh transient runtime, retained durable history.

## Initial core verification

### Playback integration verification

Windows: 107 focused Podcast tests passed (including 26 runtime cases); the full
non-native backend suite passed 978 tests, with 4 skips and 54 native/mux tests
deselected. Python compilation and diff whitespace checks passed.

An isolated Raspberry Pi directory using GStreamer 1.26.2 passed 48 native tests
(3 skipped), including LOCAL regressions and four new Podcast cases. Synthetic
localhost RSS/enclosures exercised tagged FLAC and untagged WAV, extensionless
URLs, native handoff, forced EOS fallback, shared consumers, stop/restart, refresh
recovery and accelerated decoding of 61 seconds of real PCM. Partial-PCM failure
was injected as a native bus error; it was not a physical network-disconnect test.

A bounded public smoke test of `https://feeds.megaphone.fm/darknetdiaries` found
180 entries and decoded 205,496 PCM bytes with RSS identity
`Darknet Diaries - LOW - Now Available`; shutdown completed. No RF, deployed
services or persistent user configuration were touched. This is software playback
verification, not receiver/RF acceptance.

### Earlier catalog-only verification

Fixture-based Podcast and schema tests: 88 passed on Windows and in an isolated
Raspberry Pi Python 3.13.5 directory. Full non-native backend suite: 940 passed,
4 skipped, 50 native GStreamer/mux tests deselected. Python compilation and
`git diff --check` passed; the project has no configured Python linter.

Live RSS-only validation on the Pi used two refreshes each:

- `https://feeds.megaphone.fm/darknetdiaries`: 180 episodes, title and UTC dates.
- `https://www.thisamericanlife.org/podcast/rss.xml`: 15 episodes, title and UTC dates.
- Together: 195 discovered, 10 eligible with `max_episodes=5` per feed.
- `https://feeds.bbci.co.uk/p02nq0gn.rss` returned `PODCAST_FETCH_FAILED`; the
  accompanying Darknet Diaries feed remained usable on both attempts.

Enclosure URLs were parsed and validated, not downloaded or played. No application
deployment, service restart, persistent user-data modification or RF operation was
part of this verification.
