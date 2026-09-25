# File Audio Profiles: integrated storage, API and audio runtime

Sources select `processor_mode` (`bypass`, `builtin`, `stereotool`), independent
`input_gain_db` (default 0, maximum +20 dB), and `audio_profile_path`.
Bypass requires null path and applies Source Gain only. Built-in requires a valid
shared JSON file and applies Source Gain -> Profile Input Gain -> the fixed modular chain. Stereo Tool is modeled but explicitly rejected on start/test; the UI
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

## JSON schema 1

The current eleven-stage modular structure is the first and only supported profile
schema. See [Modular DSP implementation](modular-dsp-implementation.md) for exact
fields, mappings and stage order. Historical two-stage documents, version 2,
compatibility metadata and deleted implementation identifiers are rejected.
There is no conversion on read or write. Save/Clone return version 1 at stable paths.
The shipped `default/basic.json` is a filter-only modular starting point, not a
preserved historical processing chain or the operator's tuned custom preset.

## REST

All paths below start with `/api/v1/audio-profiles/files`. The old numeric-ID `/api/v1/audio-profiles/{id}` and DB collection routes are removed.

| Method/path | Operation |
|---|---|
| GET empty suffix | List both roots |
| GET `/dsp` | Neutral template, stage schemas and local plugin availability |
| GET `/{kind}/{filename}` | Read validated profile |
| POST `/custom` | Create, body = structured schema-1 document |
| PUT `/custom/{filename}` | Replace complete custom document |
| POST `/{kind}/{filename}/clone` | Clone, body = `{"name":"New name"}` |
| DELETE `/custom/{filename}` | Delete; 204 |

There are no default PUT/DELETE routes (405); storage also rejects direct default
mutation with 403 `READ_ONLY_PROFILE`. Create/clone return 201; get/save return 200:

```text
{name, path: "custom/my_profile_<timestamp>.json", kind: "custom",
 profile: {schema_version: 1, name, stages: { /* eleven stages */ }}}
```

The `profile` member is the full validated object, not JSON text. List returns an array of the same
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

## Database schema 1

Fresh databases directly create the current Sources/Modulators/SystemConfig layout
as version 1, including the fixed-modulator triggers and Source ID sequence.
There are no profile rows or Source profile foreign keys. Source mode, gain and
stable file path remain in Source JSON. Unsupported versions and incorrect structures
fail explicitly without conversion. There is no runtime migration machinery.

## Validation and limitations

Tests cover file CRUD, strict content/path validation, collision/atomic writes,
reference protection, direct schema creation, JSON-to-pipeline settings, independent
gains, shared output ownership, audio-only restart, partial results and UI behavior.
Native GStreamer tests verify gains and negotiated DSP ordering where GI/plugins
are available; mock/builder tests do not establish acoustic or hardware results.
No physical RF/audio testing, deployment or flash was performed.

Deferred: Stereo Tool runtime/presets, live editing and
automatic external-file reload. The processing path uses F32LE DSP domain
and S16LE/48 kHz/stereo output; no general float-domain redesign was introduced.

Current validation is recorded in [pre-production cleanup](preproduction-schema-cleanup.md).
