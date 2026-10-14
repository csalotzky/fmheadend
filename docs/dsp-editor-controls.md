# DSP editor controls and Source hot-save

Current Gate/Expander superseding note (2026-10-07): the original wideband
implementations audited below have been replaced by the x8 multiband versions.
The editor consumes their generated schemas through the existing crossover and
band-card controls. See [current mappings and preset preparation](modular-dsp-implementation.md#multiband-gate--expander-schema-1).
Gate hold now maps to `th-N`, Expander hold to `ht-N`; their controls and meters
are per active band. This does not change the compressor or its editor.

## Range audit (2026-09-25)

Read-only inspection on `ssh fmheadend`: GStreamer **1.26.2**, LSP LV2
**1.2.21-5**, `gstreamer1.0-plugins-bad` **1.26.2-3+rpt3+deb13u2** and
`gstreamer1.0-plugins-good` **1.26.2-1+deb13u2**. Inspected `gst-inspect-1.0`,
GObject property min/max/default values and installed metadata under
`/usr/lib/lv2/lsp-plugins.lv2/*.ttl`. Each retained factory was instantiated in an
isolated Python/GStreamer process; inspected property defaults were set and read
back. No pipeline was connected to the headend, no package was installed and no
Pi profile/database/service was changed. These were property checks, not an RF or
audio-quality test.

Official developer manuals cross-checked for units and meaning:

- [Volume](https://gstreamer.freedesktop.org/documentation/volume/index.html):
  `volume` is a linear factor; the existing adapter uses `10^(dB/20)`.
- [Chebyshev](https://gstreamer.freedesktop.org/documentation/audiofx/audiocheblimit.html):
  cutoff in Hz; odd pole counts round upward, so the editor offers even 2–32 only.
- [Expander](https://lsp-plug.in/?page=manuals&section=expander_stereo) and
  [Compressor](https://lsp-plug.in/?page=manuals&section=compressor_stereo):
  threshold, knee and makeup use the existing linear/dB conversions; ratio is N:1.
- [Gate](https://lsp-plug.in/?page=manuals&section=gate_stereo):
  reduction is closed-gate gain. This application's positive attenuation control
  retains its existing `10^(-dB/20)` mapping; no ducking mode is added.
- [Autogain](https://lsp-plug.in/?page=manuals&section=autogain_stereo):
  LUFS target/silence controls; Grow/Fall are times per configured gain amount,
  not independent attack/release constants.
- [Parametric EQ](https://lsp-plug.in/?page=manuals&section=para_equalizer_x16_stereo):
  frequency, gain and Q; Notch retains its non-operational gain control.
- [Multiband](https://lsp-plug.in/?page=manuals&section=mb_compressor_stereo):
  independent band compression. Classic remains fixed internally, with no fake
  mode selector/status. Compressor Bypass keeps the band's audio present.
- [Limiter](https://lsp-plug.in/?page=manuals&section=limiter_stereo):
  threshold, lookahead, attack, release and stereo link retain their existing mappings.

Upstream manuals describe more modes/features than this application's verified
subset. They are reference links, not permission to use unimplemented settings.
Target wrapper metadata is authoritative for compatibility. No validation bound
or DSP mapping changed in this UX task.

### Per-field slider policy

Backend `daemon/models/dsp_editor.py` centrally supplies official documentation
URLs and editor metadata. Validation remains in `daemon/models/dsp.py`; the saved
schema-1 JSON is unchanged. The editor does not persist slider metadata.

| Fields / implementations | Supported profile range | Slider scale / gesture resolution | Native property evidence |
|---|---|---|---|
| Profile Input / Overall Gain | finite dB ≤20 | linear, 0.1 dB | volume 0–10 linear |
| Output Trim | finite dB ≤0 | linear, 0.1 dB | attenuation-only application rule |
| Chebyshev cutoff | 0 < Hz <24000 | log, 3 significant digits | cutoff 0–100000; profile stays below 48 kHz Nyquist |
| Chebyshev poles | even 2–32 | select, no slider | native 2–32, rounds odd upwards |
| Dynamics/Gate/Multiband threshold | −60..0 dBFS | linear, 0.1 dB | al/gt/al-N .001–1 linear |
| Expander/Compressor/Multiband ratio | 1..100 | log, 3 significant digits | er/cr/cr-N 1–100 |
| Dynamics/Gate/Multiband attack | 0..2000 ms | shifted log, 3 significant digits | at/at-N 0–2000 |
| Dynamics/Gate/Multiband release | 0..5000 ms | shifted log, 3 significant digits | rt/rt-N 0–5000 |
| Gate Hold | 0..1000 ms | shifted log, 3 significant digits | hold 0–1000 |
| Dynamics/Multiband knee | 0..23.99 dB | linear, 0.1 dB | kn/kn-N .0631–1 linear |
| Dynamics/Multiband makeup | −60..60 dB | linear, 0.1 dB | mk/mk-N .001–1000 linear |
| Gate reduction | 0..72 dB attenuation | linear, 0.1 dB | gr .000251–3981.073; attenuation subset only |
| AGC target | −60..0 LUFS | linear, 0.1 LUFS | level −60–0 |
| AGC maximum gain | 0..108 dB | linear, 0.1 dB | max-amp 0–108 |
| AGC silence | −84..−36 LUFS | linear, 0.1 LUFS | silence −84–−36 |
| AGC Long Grow/Fall | 10..10000 ms | log, 3 significant digits | tgrow-l/tfall-l 10–10000 |
| AGC Short Grow | 1..500 ms | log, 3 significant digits | tgrow-s 1–500 |
| AGC Short Fall | 0.1..40 ms | log, 3 significant digits | tfall-s .1–40 |
| EQ frequency | 10 ≤ Hz <24000 | log, 3 significant digits | f-N 10–24000; excludes Nyquist |
| EQ gain | −35..35 dB | linear, 0.1 dB | g-N .01585–63.09575; existing conservative subset |
| EQ Q | 0..100 | shifted log, 3 significant digits | q-N 0–100 |
| Multiband crossovers | 10..20000 Hz, ascending | log, 3 significant digits | sf-N 10–20000 |
| Limiter threshold | −48..0 dBFS | linear, 0.1 dB | th .003981–1 linear |
| Limiter lookahead | 0.1..20 ms | log, 3 significant digits | lk .1–20 |
| Limiter attack/release | 0.25..20 ms | log, 3 significant digits | at/rt .25–20 |
| Limiter Stereo Link | 0..100% | linear, 1% | slink 0–100 |

Ranges above include endpoints unless stated otherwise. Native floating-point
representations may differ in their last digits. Existing knee/EQ conservative
bounds remain intact; broadening them is unnecessary for a widget change.

Sliders use 1000 keyboard/pointer positions. Log gesture values round to three
significant digits (e.g. ~0.01 ms near 1 ms, 10 ms near 5000 ms), avoiding an
unwieldy fixed millisecond step. Shifted log uses `log(value + 0.1)` to include
real zero without inventing a positive minimum. Linear gestures round to the
table's step; endpoints are exact. Precise numeric input uses `step=any` and the
validated bounds. Loading, displaying or saving never quantizes values.

Volume's practical slider window is −60..+20 dB (trim −60..0); the valid finite dB
range has no lower bound. Cutoff's initial slider window is 0.1..23999 Hz. Windows
expand to include valid entered/saved values outside them, without clamping or
changing validation. Booleans/enums remain checkboxes/selects. Hidden/inactive
settings remain in the existing objects. Short Grow/maximum gain and Notch gain
retain their existing enabled/disabled dependencies.

## Source Save API and lifecycle

`PATCH /api/v1/sources/{id}` validates, persists and applies on the existing single
control executor. Its HTTP 200 response contains the Source fields plus:

```json
{"saved": true, "applied": false, "apply_error": "audio start failed"}
```

`applied:true, apply_error:null` means no apply error, including an idle Source
where no runtime work was needed; it is not proof that audio is flowing. For an
active Source it confirms that the replacement pipeline was constructed/initiated,
without waiting for its sender. A valid but offline HTTP/UDP/RTP/ALSA Source stays
owned and reconnects; momentary no-PCM is not a Save/apply failure. Explicit tests
still require actual PCM. Permanent construction/plugin errors and teardown failure
retain the saved-but-not-applied result; later decode errors remain in Source status.
Validation/persistence failure remains a normal error response before restart.
GET and create retain their Source response contracts.

Runtime-affecting fields are `type`, `config`, `processor_mode`, `input_gain_db`,
`audio_profile_path`, `gst_options`. Changing them replaces only that Source's
existing audio pipeline. Name-only updates to healthy Sources do not restart it.
An existing failed application can be retried by saving again, even unchanged.
Idle Sources never start because of Save.

Source Save and profile Save share `_restart_audio`: stop old worker, reuse its
`PcmOutput`, retain consumer sets and mux ownership, construct/start one replacement.
No second worker is constructed if teardown fails. Startup failure retains the
saved desired configuration, reports Source ERROR and keeps consumers attached.
Normal bounded silence applies while no PCM is produced. The frontend preserves
the saved form as pristine and reports the partial outcome beside Save.

No ModulatorManager START/STOP, TX ENABLE/CONFIG, RDS CLEAR, desired-state
mutation or STOP ALL hold release is performed. Fake-pipeline regression tests
assert output identity, multiple consumers, one replacement and no hardware
calls. This change has not been deployed or physically auditioned.

## Validation in this implementation round

- Full daemon suite: **627 passed, 28 skipped**. Skips: 21 native GStreamer tests
  (no local PyGObject/GStreamer), 5 Linux mux integration tests, 1 Linux i2c-dev
  test and 1 symlink-permission test on Windows. One pre-existing concurrent-status
  poll affected an initial lifecycle test's command-count assertion; the complete
  rerun passed without changing recovery code or that test.
- Final focused Source/profile/metadata run: **19 passed**.
- Full frontend suite: **174 passed**, 9 test files.
- TypeScript typecheck, production WebUI build and `git diff --check`: passed.
- Pi checks were read-only factory/property inspections, not deployment or a live
  Source rebuild/audio-quality test. Operator visual/audio acceptance remains.

### Repository files changed

Backend:
`daemon/api/routes.py`, `daemon/models/configuration.py`, `daemon/models/dsp.py`,
`daemon/models/dsp_editor.py`, `daemon/services.py`, `daemon/sources/dsp.py`,
`daemon/sources/manager.py`.

Backend tests:
`daemon/tests/test_audio_profiles.py`, `daemon/tests/test_backend.py`,
`daemon/tests/test_dsp_editor.py`, `daemon/tests/test_modular_dsp.py`.

WebUI (under `webui/src/app/features/`):
`audio-profiles/dsp-fields.ts`, `audio-profiles/dsp-number.ts`,
`audio-profiles/dsp-number.spec.ts`, `audio-profiles/profile-api.ts`,
`audio-profiles/profile-editor.ts`, `audio-profiles/profiles-list.ts`,
`audio-profiles/profiles.spec.ts`, `modulators/modulator-editor.html`,
`modulators/modulators.css`, `modulators/modulators.spec.ts`,
`sources/source-api.ts`, `sources/source-editor.ts`, `sources/sources.spec.ts`.

Documentation:
`docs/audio-profile-files.md`, `docs/dsp-editor-controls.md`,
`docs/fmheadend-audio-processing.md`, `docs/fmheadend-software-architecture.md`,
`docs/milestone6a.md`, `docs/modular-dsp-implementation.md`, `docs/webui-spec.md`.

The pre-existing edit to `webui/src/app/features/system/system-page.ts` was left
untouched. No profile JSON, schema version, DSP adapter mapping, Pi data, firmware,
Controller protocol, RF/RDS lifecycle or persistent desired-state behavior was changed.

Source RDS-only saves are an exception to audio retry/rebuild: they persist owned
content and hot-apply RDS followers without touching audio. Mixed saves preserve
the audio contract above. See [Source-linked RDS profiles](source-rds-profiles.md).
