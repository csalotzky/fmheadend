> M7B5 supersedes the initial European-only frequency selector with startup region plans.

# Milestone 7B2: Modulator configuration

`/modulators` summarizes the eight fixed TX IDs 0..7 with frequency, audio input,
source name, RDS configuration and reported TX state. Edit links open
`/modulators/:id`. No create/delete or additional transmitter identity exists.

The bounded editor uses one vertical RF / Audio / RDS page with Save/Cancel,
keyboard-labelled controls and the existing dark appliance styles. There are no tabs.
No autosave: dirty navigation and browser unload warn; confirmed Save marks pristine.
The shared Dashboard poll supplies observations/source names and never patches dirty forms.
Unavailable data stays explicitly unknown/stale. Runtime observations use the saved mode,
not an unsaved selection. Historical hardware error codes alone are not active faults.

## Contracts and backend change

Uses GET `/api/v1/modulators`, GET/PATCH `/api/v1/modulators/{id}`,
GET `/api/v1/sources` and the existing GET `/api/v1/status` polling. No new endpoints.
The draft derives from the existing TypeScript Modulator interface and omits identity,
timestamp and desired enabled state. `rds_metadata_to_rt` is the actual API key.

Per the reviewed policy, every active configuration PATCH returns 409 MODULATOR_ACTIVE
before mutation or hardware access. Explicit stop -> edit -> start is required.
PATCH cannot change desired TX enabled state (409 LIFECYCLE_REQUIRED); use the
existing Start/Stop endpoints. Desired ON during recovery also blocks edits.
Stopped PATCH saves configuration only. API error messages remain visible and retain edits.
This replaces the earlier implicit active stop/reapply/restart behavior for all clients.
No new RP worker, protocol change, unrelated schema change or hardware mapping was introduced:
protocol remains 1, SQLite is now 9 following the focused RF representation correction.

## RF and Audio

Frequency is a required select generated from integer kHz: 87500..108000 inclusive,
100 kHz raster, displayed with exactly one decimal MHz digit. Unconfigured or
out-of-raster saved values have a disabled Select frequency placeholder, never an
automatically selected default. Region-dependent limits/raster are a future policy TODO.
RF output offers exactly 16 relative levels: Maximum, -1.5, -3.0, ... -22.5 dB.
`rf_attenuation_db` stores positive attenuation (0 means Maximum). Fresh schema-1 Modulators have null attenuation.
Users must explicitly select a level; the UI does not display uncalibrated dBµV.
The pre-release RP protocol remains 1 with unchanged sizes and relative tenths at
the existing RF offsets. IPOW is unchanged; final board calibration is future work.
Stereo/Mono and 50/75 µs pre-emphasis belong to RF configuration.

Digital source selection lists reusable HTTP/UDP/RTP/ALSA sources; it has no independent
enable policy. ANALOG clears the source association and selects this TX's dedicated jack.
Switching back requires selecting a digital source again. No source/profile applies to analog.

Automatic gain disables the visible digital trim and (in ANALOG) analog gain controls
without erasing their values. Manual enables them. Analog gain is hidden for DIGITAL,
retained internally, and uses the existing signed-tenths high-level range; final hardware
representability depends on future RP firmware's board-fixed mapping. Digital trim has
only 0 and +1 dB. Soft clipping is independent and has only On/Off.

Current analog `input_saturated=true` shows overload; false shows a restrained no-current-
saturation observation; null/DIGITAL/stale status never shows a fake healthy indication.
No RF measurement or actual audio-content claim is made from consumed-byte flow counters.

## RDS

RDS enable preserves the disabled visible configuration. PI is normalized to four hex
digits, ECC to two (blank -> null), with optional 0x input. European PTY choices are
0..29. Existing diagnostic PTY values are retained internally, with a disabled generic
placeholder rather than offering them in the normal selector. TP is configurable.

PS has add/edit/remove/up/down controls and duration. One entry is static; multiple
entries form a carousel. The last entry cannot be removed. Manual text is validated
without silently truncating it. Existing backend/encoder character policy is unchanged.
AF has ordered add/edit/remove controls in MHz, serialized as kHz: Method A, max 25,
87.6..107.9 MHz at 0.1 MHz intervals. Obvious duplicates are rejected before submission.
Fallback RT and Use source metadata for RadioText are persistent modulator fields.
Override > enabled metadata selection > fallback runtime precedence is unchanged.

Fill from source is a manual unsaved convenience: PI = FF00 + TX ID; static PS is the
first eight NFC-normalized Unicode code points of the selected source name; fallback RT is the first
64. Setting PS from the source occurs through this action, not a second automatic path.
It confirms before replacing a multi-entry carousel and never changes metadata selection.
No source/name/metadata refresh automatically updates these fields.

`values.ts` preserves Unicode for Fill from source instead of mirroring the RDS
encoder. It NFC-normalizes and limits code points, replacing controls/non-space
separators with `?` so the draft passes the existing printable-text validation.
Supported accents are not erased. Linux alone applies the basic RDS mapping and
encoded-byte limits; see [character policy](milestone5.md#character-policy).

## Validation and smoke commands

```sh
cd webui
npm test
npm run typecheck
npm run build
npm run format:check
```

Build includes strict Angular template checks. Backend:
`python -m pytest daemon/tests -q`; native mux: `ctest --test-dir <mux-build> --output-on-failure`.
Focused tests cover exact units/hex/text, all eight identities, RF/Audio/RDS form behavior,
rejected saves, dirty navigation, polling isolation, AF/PS list edits and manual fill.
Backend tests verify active edits have no persistence or hardware side effects.

Use an isolated temporary DB/runtime with native mux `--transport mock` and fmheadendd
`--mock-rp`, following the M7B1 smoke setup. Point an untracked Angular dev proxy at it.
Configure a source, open Modulators, edit TX0, select digital input and exercise Fill from
source. Save, then explicitly start via Dashboard. A subsequent editor Save must return
409 without stopping TX. Explicitly stop, save manual analog settings, then start again.
Inspect overload/unknown behavior with mocked status fixtures; inspect desktop/narrow
layout and dirty Cancel. Remove all isolated processes and data afterward.

This run used real Pi fmheadendd, native GStreamer ALSA capture and native mux with mock
RP/SPI in an isolated DB. RF/audio/RDS round-trip, digital start, active rejection,
explicit stop/edit/start and analog status were checked through REST. Browser automation
could not initialize due to the Windows tool sandbox; visual browser validation remains
manual. No physical RP/QN transmission was tested.

Remaining hardware work: actual board gain representability and saturation observations.
No firmware, raw gain UI, full character-set redesign, extra RDS services, WebSockets,
authentication, performance milestone, Audio Profile or System management was implemented.

### Editor control refinements

Analog gain uses the board's documented twelve gains (-1.5 through +15.0 dB,
1.5 dB steps). The high-level API remains unchanged; stored gains are preserved
when disabled. Soft clipping remains available for both input modes.
PS duration fields appear only when multiple entries exist, retaining hidden
values. PI, ECC, PS and fallback RT use monospace text. PS/RT counters follow
existing Unicode code-point validation (8/64 limits), without changing encoding
or truncating edits. AF selections cover 87600..107900 kHz in integer 100 kHz
steps, displayed as MHz with one decimal place.
