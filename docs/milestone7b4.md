# Milestone 7B4 - native GStreamer Basic

## Scope and ownership

One processor runs per active digital InputSource, before shared PCM fanout.
AudioProfile gain remains independent of processor. NONE remains gain-only and
instantiates no Basic DSP stages. ANALOG, QN audio controls, RP protocol, muxd,
RDS and source lifecycle ownership are unchanged. WebUI Basic configuration is included in the Audio Profile editor.

## Exact chain

Decoded audio -> audioconvert -> audioresample -> capsfilter
(F32LE, 48000 Hz, two channels, interleaved) -> volume (common gain) ->
audiocheblimit high-pass -> equalizer-3bands -> audiodynamic compressor ->
audiocheblimit low-pass -> rglimiter -> audioconvert -> capsfilter
(S16LE, 48000 Hz, two channels, interleaved) -> bounded queue -> appsink.

Disabled high-pass, compressor, low-pass and limiter stages are omitted entirely.
EQ remains three fixed bands; zero gains are neutral. Gain has no compensation
stage after the limiter. Samples are processed only by native GStreamer elements,
not Python. The existing quick appsink callback and bounded output remain unchanged.
Required enabled-stage plugins missing at startup produce a clear dependency
error; there is no silent bypass. The audited Pi has all four plugins installed.

## Typed AudioProfile `basic_config`

| Field | Values | Default |
|---|---|---|
| highpass_enabled | boolean | true |
| highpass_hz | 20,30,40,50,60,80,100 Hz | 40 |
| eq_low_db / eq_mid_db / eq_high_db | -24..12 dB, 0.5 dB steps | 0 each |
| compressor_enabled | boolean | true |
| compressor_threshold_dbfs | -30..-3 dBFS, integer | -12 |
| compressor_ratio | 1,1.5,2,3,4 (R:1) | 2 |
| lowpass_enabled | boolean | true |
| lowpass_hz | 12000,13000,14000,15000,16000 Hz | 15000 |
| limiter_enabled | boolean | true |

Both filters use audiocheblimit type 1, four poles, ripple zero (Butterworth-like).
EQ bands are the native fixed 100 Hz, 1.1 kHz and 11 kHz bands. Compressor uses
mode compressor and soft-knee; threshold is `10 ** (dBFS / 20)` and ratio is `1/R`.
This is the element's amplitude-domain curve, not a time-envelope broadcast
compressor or an assertion of logarithmic slope equivalence. No attack/release
controls are provided. Native deterministic tests confirm direction, unity ratio,
sign symmetry, unaffected quiet samples and reference soft-knee sample values.

rglimiter uses its fixed -6 dB threshold and native soft-knee hard limiting.
It is a simple safety limiter, not a broadcast composite limiter. No extra gain,
threshold or timing controls are invented. QN still owns 50/75 microsecond
pre-emphasis; no FM pre-emphasis or MPX/composite processing is added here.

Native contracts: [audiodynamic](https://gstreamer.freedesktop.org/documentation/audiofx/audiodynamic.html),
[rglimiter](https://gstreamer.freedesktop.org/documentation/replaygain/rglimiter.html).

## Compatibility and API

The existing AudioProfile CRUD accepts `basic_config` as a typed object. PATCH
replaces that object; omitted fields within it take model defaults. Omitting the
whole object retains the current settings on PATCH, so clients that omit Basic settings retain compatibility. Settings persist independently of processor
choice. NONE ignores them without erasing them.

Existing JSON without the object gets defaults on read; the next save persists
it. SQLite remains schema 9, with no schema rewrite needed. Legacy GST_BASIC
profiles now activate the documented default stages instead of gain only.
Legacy GSTREAMER/bypass normalization follows M7B3. Legacy EQ/compressor/limiter/
advanced blobs remain preserved, not guessed into settings; nonempty blobs with
GST_BASIC fail explicitly until cleared through the existing API. They have no
second effective DSP configuration path.

All existing-profile saves still return 409 when any referencing InputSource is
assigned to a Modulator whose current runtime state is ON, including name, gain,
processor and Basic settings. No live updates or hidden restart occur. Referenced
profile deletion stays blocked. No RP protocol or database version bump.

## Validation and limitations

Focused Linux command (one line):

```sh
python -m pytest daemon/tests/test_basic.py daemon/tests/test_audio_profiles.py daemon/tests/test_gstreamer.py -q
```

Native tests use PyGObject, local WAV/HTTP fixtures and appsrc/appsink float samples;
no gst-launch subprocess, public radio stream, RP hardware or RF measurement.
Plugin/property availability was inspected on the Pi via native Gst. No new system
packages or Python dependencies were needed. Frontend validation uses the focused Audio Profiles tests and one production build.

The chain uses small native IIR filters, three EQ bands and sample-domain dynamics;
it is intended for Zero 2 W-class operation once per source. Eight distinct source
pipelines remain supported by the existing architecture. Full-system CPU/soak
benchmarking and subjective audio-quality evaluation were not performed.

Deferred: multiband/FIR/FFT processing, LSP/LV2/Stereo Tool,
arbitrary chains, attack/release, FM pre-emphasis, composite limiting and analog DSP.

## WebUI integration

The existing centered Audio Profile editor displays a compact Basic processing
section only for GST_BASIC. It uses the exact backend options/defaults above,
with three numeric EQ gains in 0.5 dB steps. Off disables dependent cutoff or
compressor controls without erasing values. Switching to NONE hides the section
without clearing it; saved payloads preserve disabled settings. Existing profiles
use API-returned settings directly. Save/Cancel, dirty protection and backend 409
reporting remain unchanged. No element names or raw processing properties are
exposed. Frontend options are centralized in `basic-config.ts`.

Frontend commands: `npm test -- --include='src/app/features/audio-profiles/*.spec.ts'`
and `npm run build`. No backend changes are required by the UI integration.
