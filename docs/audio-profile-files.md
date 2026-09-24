# File Audio Profiles: integrated storage, API and audio runtime

Sources select `processor_mode` (`bypass`, `builtin`, `stereotool`), independent
`input_gain_db` (default 0, maximum +20 dB), and `audio_profile_path`.
Bypass requires null path and applies Source Gain only. Built-in requires a valid
shared JSON file and applies Source Gain -> Profile Input Gain -> existing Basic
processing. Stereo Tool is modeled but explicitly rejected on start/test; the UI
shows it disabled/coming soon. There is no silent processing fallback.

The existing Audio Profiles and Source pages use file paths end to end. SQLite
stores Sources and their path references, not AudioProfile entities. Numeric-ID
profile routes and the old table are removed. Profile names can change independently
of their stable filenames. Multiple Sources share one file without copies.

## Paths and installation

| Role | Directory |
|---|---|
| Git-tracked repository defaults | `audio-profiles/default` |
| Installed read-only defaults | `/usr/share/fmheadend/audio-profiles/default` |
| Persistent custom files | `/var/lib/fmheadend/audio-profiles/custom` |

Defaults are application data installed separately, following the existing manual
deployment pattern (like WebUI assets), not embedded in the Python wheel. From
the repository root, the operator can install them with:

```sh
sudo install -d -m 0755 /usr/share/fmheadend/audio-profiles/default
sudo install -m 0644 audio-profiles/default/*.json /usr/share/fmheadend/audio-profiles/default/
sudo install -d -o fmheadend -g fmheadend -m 0750 /var/lib/fmheadend/audio-profiles/custom
```

These commands were not executed by this task. The daemon never writes defaults.
The existing systemd StateDirectory owns `/var/lib/fmheadend`; custom storage is
created lazily if absent. Missing default storage is an explicit storage error,
not a silent empty/default fallback. Bypass Sources do not access profile storage.
File API calls and Built-in Source validation/start/status load the selected files;
Built-in Sources fail explicitly if their selected file is missing.

Development example (from the checkout):

```sh
fmheadendd --mock --database ./fmheadend.db \
  --audio-profiles-default-dir ./audio-profiles/default \
  --audio-profiles-custom-dir ./audio-profiles/custom
```

Both roots also exist in `Settings` for tests/application factories. Custom files
under `audio-profiles/custom/` are ignored by Git. Keep the roots and their parent
directories under trusted operator/daemon ownership; they are not upload directories
for other local users. Symlinks/junctions in profile paths are rejected, including
root/ancestor links. Logical names are restricted to `default|custom` plus one
ASCII alphanumeric/underscore/hyphen filename ending in `.json`. No arbitrary paths.

## Schema 1

The complete initial example is [basic.json](../audio-profiles/default/basic.json).
The shape is:

```json
{
  "schema_version": 1,
  "name": "GST Basic",
  "stages": {
    "gain": {
      "selected": "GST_VOLUME",
      "implementations": {"GST_VOLUME": {"gain_db": 0}}
    },
    "processor": {
      "selected": "GST_BASIC",
      "implementations": {"GST_BASIC": {"highpass_hz": 40}}
    }
  }
}
```

The abbreviated Basic settings above expand through the existing typed defaults;
the shipped file contains them all explicitly: HP enabled/40 Hz; EQ 0/0/0 dB;
compressor enabled/-12 dBFS/2:1; LP enabled/15000 Hz; limiter enabled. Gain is 0 dB.
Ranges, fixed element properties and DSP ordering are unchanged from
[M7B4](milestone7b4.md). Gain precedes processing. `processor.selected` supports
`NONE` and `GST_BASIC`; the saved `implementations.GST_BASIC` object remains present
even under `NONE`. Thus selecting bypass does not erase saved settings.

These are the two currently implemented processing units. The selected/settings
envelope can be extended to additional modular stages and validated implementations
later; no unimplemented plugin identifiers, parameter bags or defaults are accepted
now. Unknown fields, unsupported versions/implementations and invalid settings fail
validation, including inactive settings. Name is editable; path is immutable.

## REST

All paths below start with `/api/v1/audio-profiles/files`. The old numeric-ID `/api/v1/audio-profiles/{id}` and DB collection routes are removed.

| Method/path | Operation |
|---|---|
| GET empty suffix | List both roots |
| GET `/{kind}/{filename}` | Read validated profile |
| POST `/custom` | Create, body = structured schema-1 document |
| PUT `/custom/{filename}` | Replace complete custom document |
| POST `/{kind}/{filename}/clone` | Clone, body = `{"name":"New name"}` |
| DELETE `/custom/{filename}` | Delete; 204 |

There are no default PUT/DELETE routes (405); storage also rejects direct default
mutation with 403 `READ_ONLY_PROFILE`. Create/clone return 201; get/save return 200:

```json
{
  "name": "My profile",
  "path": "custom/my_profile_20260924213515.json",
  "kind": "custom",
  "profile": {
    "schema_version": 1,
    "name": "My profile",
    "stages": {
      "gain": {"selected": "GST_VOLUME", "implementations": {"GST_VOLUME": {"gain_db": 0}}},
      "processor": {"selected": "GST_BASIC", "implementations": {"GST_BASIC": {}}}
    }
  }
}
```

The `profile` member is the full validated object, not JSON text (the empty Basic
object above abbreviates the full expanded settings). List returns an array of the same
metadata without `profile`. Save/clone return the actual stable path; never rebuild
it from the display name. Unicode NFKD transliteration generates ASCII slugs,
with `profile` fallback, UTC timestamp and numeric collision suffix. Creation uses
an atomic no-overwrite link of a complete temporary file; save uses atomic replace.
Files are flushed/fsynced before publishing; temporary files are removed on failure.

Errors use the existing `{error,message}` envelope: missing file 404
`PROFILE_NOT_FOUND`; invalid stored JSON/schema 422 `INVALID_PROFILE`; bad logical
path 422 `INVALID_PROFILE_PATH`; request validation 422 `VALIDATION_ERROR`; storage
I/O failure 503 `PROFILE_STORAGE_ERROR`; referenced deletion 409 `PROFILE_IN_USE`.
Reads are bounded to 64 KiB. Listing fails explicitly on invalid files instead of
silently hiding them or substituting DSP defaults. Invalid custom files can be
repaired by a valid full PUT or deleted.

Deletion checks every persisted Source path, including stopped Sources. Referenced custom profiles return 409; references are never reassigned automatically.

## Save and audio lifecycle

A custom PUT is serialized on the existing control executor: validate schema and
required selected plugins, atomically save, then restart active Sources referencing
that path. Creation validates plugins but restarts nothing. Clone copies validated
settings; plugin availability is checked before actual start or later Save.

Each Source remains one pipeline regardless of TX count. The existing `PcmOutput`
fanout and consumer sets survive replacement; mux producers retain their object
and sockets. Native stop clears queued PCM; missing audio yields the usual bounded
silence. Restarting audio creates no TX OFF/ON, QN config, RDS CLEAR, desired-state
change or STOP ALL hold release. A failed stop retains ownership so a second
worker is not created over a worker that may still be alive. Failures are isolated
per Source; subsequent Save can retry.

PUT returns the usual profile result plus `saved: true`, `applied: boolean`, and
`restart_errors: {source_id: message}`. HTTP 200 with `applied:false` means the
validated file was saved but audio restart failed. The UI keeps the saved form,
shows the partial outcome next to Save, and never claims it was applied. Validation,
missing file and plugin preflight failures occur before save, using normal errors;
missing plugins report 503 `PROFILE_PLUGIN_UNAVAILABLE`.

The existing stop-before-edit Source rule remains; this task changes profile Save,
not Source/TX lifecycle. A shared profile may be edited while Sources run. Invalid or
missing files discovered at start prevent that Source from starting. Periodic source
status checks report external file corruption/missing files as Source ERROR without
switching to Bypass or stopping unrelated TXs. Already-running native pipelines hold
their last loaded settings until an explicit restart/Save; no file watcher or live
parameter mutation is implemented.

## Database schema 2 migration

Startup transactionally migrates schema 1, preserving Source IDs/config/URLs,
modulator references, desired state and all unrelated configuration. Sources without
profiles become Bypass/0 dB. Referenced gain-only profiles become Bypass with their
gain moved to Source Gain. Referenced default GST Basic profiles become Built-in
`default/basic.json`, with their old gain moved to Source Gain (profile gain is 0).

Any unresolved/inconsistent reference, unknown settings/processor, non-default saved
Basic settings, or unreferenced profile row blocks the migration **without changing
the database**. No arbitrary user profile is silently discarded. Back up the DB and
explicitly export/resolve such data before retrying; this iteration does not provide
a general legacy-preset importer. The local development DB was inspected read-only:
schema 1, zero Sources, zero profiles. It was not migrated by development tooling.

Only migration code refers to the old table/foreign key; no permanent compatibility
runtime is retained. Fresh databases use schema 2 directly. File JSON stays schema 1.
Install the shipped defaults before running migrated Built-in Sources.

## Validation and limitations

Tests cover file CRUD, strict content/path validation, collision/atomic writes,
reference protection, migration rollback, JSON-to-pipeline settings, independent
gains, shared output ownership, audio-only restart, partial results and UI behavior.
Native GStreamer tests verify gains and negotiated DSP ordering where GI/plugins
are available; mock/builder tests do not establish acoustic or hardware results.
No physical RF/audio testing, deployment or flash was performed.

Deferred: Stereo Tool runtime/presets, new modular/LSP stages, live editing and
automatic external-file reload. The Basic path keeps its existing F32LE DSP domain
and S16LE/48 kHz/stereo output; no general float-domain redesign was introduced.

Integration validation: full daemon suite **619 passed / 32 skipped**; final focused
file/profile/migration/Basic suite **94 passed / 1 skipped** (including the final
Source ID high-water preservation case). Full frontend suite **160 passed**;
TypeScript typecheck, production WebUI build and `git diff --check` passed.
Skipped dependencies: 25 native GStreamer tests, five Linux mux tests, one Linux
I2C test and one Windows symlink-creation test. Existing Starlette/httpx deprecation
warning remains. No physical RF/audio testing was performed.
