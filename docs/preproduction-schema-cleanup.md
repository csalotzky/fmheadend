# First-release schemas and DSP cleanup — 2026-09-25

## Current contract

The current physical SQLite structure and current modular Audio Profile structure
are independently version **1**. This is a pre-production version-history reset,
not a downgrade or supported conversion. Fresh databases directly create Sources,
Modulators, SystemConfig, the Source ID sequence and both fixed-modulator triggers.
Opening an unsupported version, an unversioned nonempty database or an incorrect
structure fails without rewriting it. `/api/v1` is unchanged.

Removed: development database conversion, historical two-stage profile parsing,
BasicConfig, profile conversion, compatibility metadata/ordering, fixed EQ,
Basic dynamics/limiter and Windowed Sinc. Sinc is removed entirely, not a disabled
option. No retained LSP/Chebyshev parameter mapping or setting was retuned.

| Stage, in fixed order | Supported implementations |
|---|---|
| Profile Input Gain | volume |
| HPF | Off / Chebyshev |
| Gate / Expander | Off / LSP Expander Stereo / LSP Gate Stereo |
| AGC | Off / LSP Autogain Stereo |
| EQ | Off / LSP Parametric EQ x16 Stereo |
| Wideband Compressor | Off / LSP Compressor Stereo |
| Multiband Compressor | Off / LSP Multiband Compressor x8 Stereo |
| Master Gain | volume |
| Limiter | Off / LSP Limiter Stereo |
| LPF | Off / Chebyshev |
| Output Trim | volume, attenuation only |

Backend metadata and the existing editor now expose this same implementation set.
The editor uses schema 1, with no inherited-order message or discarded-plugin help.
Supported hidden/inactive settings still survive Save and implementation switches.
Save still rebuilds affected audio pipelines only; no control, RF, RDS, desired-state,
STOP ALL, source ownership or transport code changed.

The factory filename remains `default/basic.json` for stable references. Its display
name is Basic; it now directly contains eleven modular stages. Existing unity gain,
40 Hz HPF and 15 kHz LPF settings are retained (4 poles/type I/ripple 0), with removed
processors OFF. The factory is not a copy of the tuned custom preset.

## Authorized Pi data cleanup

Inspected database: `/home/fmheadend/fmheadend-play.db`.
No FM Headend daemon or database holder was detected before modification. The
actual schema matched the current DDL; SQLite's quoted `sources` identifier is
structurally equivalent. There were no application indexes to change.

Only `PRAGMA user_version` changed **2 → 1**. A SQLite backup was made first;
tables, triggers, every raw Source/Modulator/SystemConfig row and `sqlite_sequence`
compared identical afterward. Eight Sources and eight modulators remain intact.
Application snapshot SHA-256 (schema plus raw ordered rows):

`370f0d04b1b380f734aee2d2c72b9adec4d25a982eb60a6e20e3ad1b1de20f03`

Actual configured roots were confirmed from the Pi's launcher/default settings:

* Factory: `/usr/share/fmheadend/audio-profiles/default/basic.json`
* Tuned custom: `/var/lib/fmheadend/audio-profiles/custom/teszt_20260925092851.json`

Sources 1 and 6 reference that same custom file; all its selected implementations
are retained. Cleanup changed its version and removed only obsolete metadata and
discarded implementation objects. No retained setting, selected implementation,
name or stable path changed. Decimal-aware parsed comparison passed. A stronger
comparison also passed: every surviving implementation's raw JSON text, including
numeric spellings, and every selected value is identical to the backup.
SHA-256 of the ordered retained-token comparison:

`cbc8dadcb2a7d5485cd0299ad061af0dc262312e61464c6b441e537c4c7ef736`

Backups and machine-readable verification records are retained on the Pi under
`/home/fmheadend/schema-cleanup.7fXiYC/`:

* `fmheadend-play.backup.db` — consistent original SQLite backup, marker 2
* `tuned-profile.backup.json` — original custom preset
* `factory-profile.backup.json` — original factory file
* `database-result.json`, `custom-result.json`, `factory-result.json`
* `final-verification.json` — current schema validation and exact comparisons

Custom ownership/permissions remain UID 1000 / 0600. Factory remains root / 0644.
The operator executed the prepared factory backup/atomic replacement using sudo
because noninteractive sudo required a password. Subsequent read-only verification
confirmed both files validate under the current schema and match prepared contents.
The one-time cleanup script is in the backup directory, not the application.

No application code was deployed, no service was restarted, and no hardware command
was issued. **The Pi's old application code remains unchanged and must be replaced
with the matching cleaned code before its next start against these cleaned files.**
That deployment is deliberately outside this task.

## Verification

* Focused backend schema/storage/DSP/lifecycle tests: **93 passed, 1 skipped**.
* Complete backend suite: **619 passed, 28 skipped**.
* Skips: 21 native GI/GStreamer tests unavailable on Windows, five Linux mux tests,
  one Linux i2c-dev test, one Windows symlink-privilege test. Existing Starlette/httpx
  deprecation warning remains.
* Frontend: **164 passed**, eight test files; TypeScript typecheck and production
  WebUI build passed.
* `git diff --check`: passed.
* Pi: isolated retained-adapter probe passed in 8.27 s (8.04 CPU s), one stereo
  fixture at a time, no mux/device connection. Checks include all retained element
  creation/property readbacks, F32LE/48 kHz/stereo, channel isolation, LSP EQ,
  four/eight-band bypass/compression and Autogain short-time Grow.
* Pi final read-only verification: custom/factory model validation, ownership,
  permissions, protected token/value equality and DB snapshot equality all passed.

No new load/endurance or RF/audio listening test was run for this cleanup. Earlier
measurements of retained plugins remain in the implementation document.

## Dead-code inspection

Removed factory names have no active adapters, schemas, selectors or current
documentation entries. Deleted implementation identifiers occur only in negative
validation tests asserting their rejection. Version 2 and the compatibility key
likewise appear only in rejection tests or this one-time cleanup record. Statements
that runtime migration is absent are intentional. `sinc-filter-mode` remains an
unrelated existing `audioresample` source option, not an LPF implementation.

## Repository files

The exact changed/deleted file inventory follows.

```text
README.md
audio-profiles/default/basic.json
daemon/database.py
daemon/models/audio_profile_file.py
daemon/models/configuration.py
daemon/models/dsp.py
daemon/sources/basic.py
daemon/sources/dsp.py
daemon/sources/gst_pipeline.py
daemon/tests/test_audio_profile_files.py
daemon/tests/test_audio_profiles.py
daemon/tests/test_backend.py
daemon/tests/test_basic.py
daemon/tests/test_database.py
daemon/tests/test_gstreamer.py
daemon/tests/test_modular_dsp.py
daemon/tests/test_profile_migration.py
daemon/tests/test_region_system.py
daemon/tools/dsp_adapter_probe.py
daemon/tools/dsp_load_probe.py
daemon/tools/dsp_native_probe.py
daemon/tools/dsp_source_probe.py
docs/audio-profile-files.md
docs/fmheadend-audio-processing.md
docs/fmheadend-software-architecture.md
docs/milestone2.md
docs/milestone6a.md
docs/milestone7a.md
docs/milestone7b1.md
docs/milestone7b3.md
docs/milestone7b4.md
docs/milestone7b5.md
docs/modular-dsp-implementation.md
docs/preproduction-schema-cleanup.md
docs/webui-spec.md
webui/src/app/features/audio-profiles/profile-api.ts
webui/src/app/features/audio-profiles/profile-editor.ts
webui/src/app/features/audio-profiles/profiles.spec.ts
```
