# Milestone 7B3 - Audio Profiles

Backend processing capabilities described here are superseded by
[M7B4](milestone7b4.md). The M7B3 UI is unchanged; Basic controls remain deferred.

Audio Profiles belong to digital InputSources. A shared source has one processed
GStreamer pipeline and fans its PCM out to its consumers. ANALOG modulators do
not use profiles. Source gain is unrelated to QN `digital_trim_db`.

## Implemented model and audit

`name`, `gain_db` (default 0 dB), and `processor` (default `NONE`) are the editor
fields. `NONE` means gain only. `GST_BASIC` currently also provides gain only:
there is no implemented EQ, compressor or limiter chain. The editor explicitly
states this limitation. `eq_config`, `compressor_config`, `limiter_config`, and
`advanced_options` remain reserved stored fields, never editor controls. Nonempty
reserved settings with GST_BASIC fail source startup clearly rather than pretend
to process audio. NONE ignores these optional processor settings.

Gain uses the existing native GStreamer volume element, linear factor
`10 ** (gain_db / 20)`, before fixed PCM output. The existing +20 dB runtime limit
is also validated at the API boundary. Finite negative gain has no imposed lower
bound; no artificial precision/raster is added. Both processors apply gain.

Legacy records are normalized at the repository read boundary: old
`processing_enabled=true` maps to GST_BASIC, false/absent maps to NONE, and
`GSTREAMER` is no longer a current API choice. Stored gain and reserved fields
are retained. **Previously bypassed nonzero gain now applies**, as required by
the processor-independent model; inspect legacy profiles before starting audio.
The next save writes the current shape. SQLite remains schema 9, with no physical
schema change or startup rewrite. Current API requests reject the obsolete flag.

## Safety and UI

Routes: `/audio-profiles`, `/audio-profiles/new`, `/audio-profiles/:id`.
The list shows name, processor, gain and number of referencing InputSources.
The editor uses Save/Cancel and dirty navigation/unload protection. Defaults are
omitted from new requests when untouched, so the backend supplies them.

Existing GET/POST/PATCH/DELETE `/api/v1/audio-profiles` resources are reused.
DELETE requires confirmation and is rejected with 409 if any source references
it. Every existing-profile PATCH (including name-only or unchanged submissions)
is rejected with 409 if a referencing InputSource is assigned to a Modulator
whose current runtime TX state is ON. Desired enabled state alone does not block
editing. Stop all affected transmitters explicitly before saving changes.
Creating profiles remains allowed. Guidance is shown only in the existing-profile editor.
No implicit stop/restart is performed. API errors preserve editor contents.
The existing Source selector fetches current profiles when opened, so create,
rename and delete are reflected on normal navigation/reload without another cache.

## Validation and smoke

Run `pytest daemon/tests/test_audio_profiles.py daemon/tests/test_gstreamer.py
 daemon/tests/test_backend.py` on the Linux test host (as one shell line).
Run frontend `npm test -- --include='src/app/features/audio-profiles/*.spec.ts'
 --include='src/app/features/sources/*.spec.ts'` and `npm run build`.
Native gain tests use a local WAV HTTP fixture and verify half-amplitude samples
for both processors. API safety tests use mocked source pipelines; no RF claim.

Manual: create a -6 dB NONE profile, assign it in Sources, then start a digital
source using mock RP/SPI. Inspect the profile count; attempt a gain edit while
active (409), attempt renaming (409), stop all affected transmitters, save and restart.
Try deleting the referenced profile (409). Remove the association before deletion.

## Deferred

GST_BASIC DSP expansion is future work. LSP would use external plugin chains and
preset/config files; Stereo Tool would use external configuration (potentially
.sts files). Neither is selectable or implemented. No DSP workstation, plugin GUI,
LV2 host, arbitrary pipeline editor, analog processing or RP protocol changes.
