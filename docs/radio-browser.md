# Radio Browser backend

HTTP Sources accept `config.radio_browser_uuid` (UUID string or null) alongside
`config.uri`. Null/omitted means a manually managed stream. No additional station
metadata is persisted. Schema version and Source type are unchanged.

## Read-only directory API

All paths below start with `/api/v1/radio-browser`:

| GET path | Query parameters | Response |
|---|---|---|
| `/stations/search` | `name`, optional `countrycode`, `tag`, `offset=0`, `limit=25` (max 100), `hidebroken=true` | `{stations: [...], offset, limit}` |
| `/stations/{station_uuid}` | UUID path parameter | One station; 404 if absent |
| `/countrycodes` | `offset=0`, `limit=250` (max 250) | `[{name: "AT", stationcount: 123}]` |
| `/tags` | optional substring `name`, `offset=0`, `limit=25` (max 100) | `[{name: "jazz", stationcount: 123}]` |

Offsets are limited to 100000. A station contains `stationuuid`, `name`, `url`,
`url_resolved`, `playable_url`, `countrycode`, `country`, `tags` (string array),
`codec`, `bitrate`, `favicon`, `lastcheckok` (boolean/null), and `hls` (boolean).
For example, `GET /api/v1/radio-browser/stations/search?name=Jazz&countrycode=AT&limit=10`
returns `{"stations": [], "offset": 0, "limit": 10}` when there are no matches.
These endpoints never save, test, or restart Sources. Invalid parameters use the
usual 422 response; upstream failures use 503 `RADIO_BROWSER_UNAVAILABLE` with
sanitized messages.

Create a linked Source through the existing `/api/v1/sources` POST, for example:

```json
{"name":"Radio","type":"HTTP","config":{"uri":"https://example.org/live","radio_browser_uuid":"960e57c5-0601-11e8-ae97-52543be04c81"}}
```

Unlink without changing the URI using PATCH `/api/v1/sources/{id}`:
`{"config":{"radio_browser_uuid":null}}`. A UUID-only patch merges this one field;
other config objects retain existing replacement semantics. Manual URI edits are
permitted even when a UUID is supplied.

## URL policy and bounded networking

Prefer the original HTTP(S) `url` for durable redirects and HLS. Recognizable plain
station playlists (`.m3u`, `.pls`, `.asx`, `.xspf`, unless marked HLS) use
`url_resolved`, because the existing ingest does not parse these station playlists.
If the original is unsupported, use a valid HTTP(S) resolved URL. Otherwise
`playable_url` is null. Resolution is not a quality choice. Opaque playlist URLs
cannot be identified reliably from directory metadata and may fail normal ingest.

The client uses the official `all.api.radio-browser.info` DNS/PTR discovery,
HTTPS-only official hostnames, and fallback mirrors if discovery fails. Discovery
is cached for 15 minutes, not station data. Requests have a 15-second overall bound,
4-second HTTP timeout (3-second connect), at most two mirror attempts, four parallel
requests, and a 2 MiB uncompressed JSON response limit. No server URL is accepted
from API callers. Reference: https://docs.radio-browser.info/.

## Recovery ownership and policy

Only linked active HTTP Sources with consumers participate. `GstPipeline._run`
uses its existing `RetryBackoff.attempt`: recovery becomes eligible after three
failed attempts (existing waits 1, 2, then 5 seconds). Thus one transient failure
does not initiate lookup. The existing reset after 30 seconds of sustained PCM is
unchanged. Permanent construction/configuration errors retain their existing fatal
behavior; the directory cannot repair missing plugins or invalid DSP configuration.

An ASGI task observes eligibility through the control executor once per second.
Network requests run outside the control executor and Source/GStreamer workers.
There is one ticket per shared Source, at most eight outstanding lookup tasks,
and a five-minute per-Source cooldown after an outcome. Ordinary reconnects
continue independently. Lookup handoffs expire after 60 seconds if control fails.
No lookup occurs at normal startup, during healthy playback, or for one-shot probes.

A new URL for exactly the linked UUID is queued into the existing retry loop.
The same Source, processing settings, PCM output and TX consumers are retained.
Actual validated PCM drained by the Source worker is required before committing;
HTTP success and PLAYING alone are insufficient. Codec, bitrate and upstream health
flags do not veto a playable recovery. Failed/unchanged/missing candidates retain
the saved URL. Candidate cancellation returns the worker to the saved URI.

Control verifies ownership, consumers and the complete saved Source revision
before applying or committing. SQLite atomically compares that revision, UUID and
old URI, then updates only URI and timestamp. Edits, unlinking, deletion, shutdown
and pipeline replacement invalidate stale work. If storage fails after confirmed
PCM, audio continues and only the guarded write retries every 30 seconds; failure
is explicitly logged. Recovery logs include Source IDs and outcomes, never URLs
or credentials. No database migration, frontend, or separate playback loop is used.
