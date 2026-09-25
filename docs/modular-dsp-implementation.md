# Modular Built-in DSP implementation — 2026-09-25

The approved design remains [fmheadend-audio-processing.md](fmheadend-audio-processing.md).
This document records implementation and measurement, not an RF acceptance report.

## Implemented

The existing file API, Source selection, ownership, output fan-out and audio-only
Save lifecycle are retained. No TX/QN/RDS, mux, SPI or protocol code changes.
The editor has eleven collapsible stages in the approved order:

`Input Gain → HPF → Gate/Expander → AGC → EQ → Wideband → Multiband → Master Gain → Limiter → LPF → Output Trim`

Source Gain precedes this chain independently. Built-in processing negotiates
F32LE/48 kHz/interleaved stereo, then converts once to the existing S16LE/48 kHz
stereo output. Bypass still applies Source Gain; Stereo Tool still fails explicitly.
Master Gain is before limiting; Output Trim is attenuation-only after LPF.

The schema is JSON version **2**. SQLite remains version **2**, unchanged.
Every stage has `selected` and `implementations`; saved settings for inactive
implementations are validated and preserved. `OFF` creates no element. A stage's
settings are not erased by switching it off. No arbitrary native property bag is
accepted. Authoritative fields, bounds and defaults are in `daemon/models/dsp.py`.

Example shape (abbreviated; all eleven stages are required):

```json
{
  "schema_version": 2,
  "name": "My processing",
  "compatibility": null,
  "stages": {
    "input_gain": {"selected":"VOLUME","implementations":{"VOLUME":{"gain_db":0}}},
    "hpf": {"selected":"CHEBYSHEV","implementations":{"CHEBYSHEV":{"cutoff_hz":30,"poles":4,"ripple_db":0,"type":1}}},
    "gate": {"selected":"OFF","implementations":{}},
    "agc": {"selected":"OFF","implementations":{}},
    "eq": {"selected":"OFF","implementations":{}},
    "wideband": {"selected":"OFF","implementations":{}},
    "multiband": {"selected":"OFF","implementations":{}},
    "master_gain": {"selected":"VOLUME","implementations":{"VOLUME":{"gain_db":0}}},
    "limiter": {"selected":"RGLIMITER","implementations":{"RGLIMITER":{}}},
    "lpf": {"selected":"CHEBYSHEV","implementations":{"CHEBYSHEV":{"cutoff_hz":15000,"poles":4,"ripple_db":0,"type":1}}},
    "output_trim": {"selected":"TRIM","implementations":{"TRIM":{"gain_db":0}}}
  }
}
```

`GET /api/v1/audio-profiles/files/dsp` supplies the neutral creation template,
ordered stage choices, validated field descriptions/defaults and **daemon-local**
factory availability. It is editor metadata in the existing API family, not a new
profile store. Available does not certify every possible parameter combination or
eight-programme load. Save/start instantiate selected factories, set parameters
and verify native readback; negotiation/start failures remain Source errors.

### Basic compatibility

The shipped `default/basic.json` stays unchanged on disk. V1 files are strictly
validated and converted on read; GET never writes them. Save/Clone emit version 2
at the same stable path (Clone gets its existing newly generated path).
`compatibility: "gst_basic_v1"` preserves the inherited LPF-before-limiter order
and `equalizer-3bands`. Existing HPF 40 Hz, EQ gains/frequencies, compressor
threshold/slope/knee, LPF 15 kHz and limiter settings are retained. Added Master
Gain/Output Trim are unity. The original native chain and the converted default
produced **sample-identical F32 output** in the isolated Pi test (max difference 0).
No implicit conversion to `equalizer-nbands` occurs.

New profiles have `compatibility: null`, HPF 30 Hz/4 poles, flat three-band
`equalizer-nbands`, Gate/AGC/compressors Off, Basic limiter, Chebyshev 15 kHz/4 poles,
and three unity gains. These are neutral engineering starting settings, not a
broadcast preset. Their LPF is after the limiter. Clones of Basic retain the
explicit compatibility marker; the editor identifies that ordering exception.

## Verified native mappings

LSP factory prefix below is `lsp-plug-in-plugins-lv2-`; all listed suffixes end
`-stereo`. Installed LV2 wrappers use **`enabled=true`** to process; false bypasses.
Online older wrapper documentation showing `bypass` is not used as the mapping.

| Stage / implementation | Exact properties / conversion |
|---|---|
| Three profile gains / `volume` | `gain_db → volume = 10^(dB/20)`; max +20 dB, Output Trim max 0 dB |
| HPF/LPF Chebyshev / `audiocheblimit` | `cutoff_hz→cutoff`, `poles→poles`, `ripple_db→ripple`, `type→type`; mode 1 HPF, 0 LPF. Even poles 2–32; cutoff strictly between 0 and 24 kHz. Type/ripple hidden but preserved |
| Basic Expander/Compressor / `audiodynamic` | `threshold_dbfs→threshold=10^(dBFS/20)`, `ratio→ratio` **native amplitude slope**; mode 1/0; hard/soft→characteristics 0/1. No Attack/Release/Makeup. Old Basic UI ratio R converts to slope 1/R |
| GStreamer EQ / `equalizer-nbands` | `num-bands`, child `freq`, `gain` dB, **`bandwidth` Hz**, `type`; 0–16 saved bands; no bands omits EQ |
| Legacy EQ / `equalizer-3bands` | low/mid/high→`band0/1/2`, preserving original native frequencies/filter types |
| Basic limiter / `rglimiter` | `enabled=true`; no invented controls. Fixed −6 dB threshold is **not** an output ceiling |
| Expander / `expander-stereo` | downward/upward→`em=0/1`; threshold→`al` linear, ratio→`er`, knee dB→`kn=10^(-dB/20)`, attack/release→`at/rt` ms; makeup→`mk` linear |
| Gate / `gate-stereo` | threshold→`gt` linear; positive attenuation `reduction_db→gr=10^(-dB/20)`; attack/hold/release→`at/hold/rt` ms; hidden zone/hysteresis→`gz/gh/ht/hz` |
| AGC / `autogain-stereo` | target→`level` LUFS; max enable/gain→`max-on/max-amp` dB; silence→`silence`; long grow/fall→`tgrow-l/tfall-l` ms; short fall/grow→`tfall-s/tgrow-s` ms; short Grow enable→`qamp` (Off by default); K weighting `weight=5`, internal `scmode=0` |
| Wideband / `compressor-stereo` | downward `cm=0`; threshold→`al` linear; ratio→`cr`; `at/rt` ms; knee→`kn` linear; makeup→`mk` linear. Hidden hold/release threshold/stereo split/dry/wet→`hold/rrl/ssplit/cdr/cwt/cdw` |
| LSP EQ / `para-equalizer-x16-stereo` | IIR `mode=0`; Bell/Low Shelf/High Shelf/Notch→`ft-N=1/5/3/6`; frequency→`f-N`, gain→`g-N` linear, Q→`q-N`, RLC(BT) `fm-N=0`, slope `s-N=0`. All unused slots explicitly `ft-N=0`; never mute/solo to remove a filter |
| Multiband / `mb-compressor-stereo` | Classic `mode=0`; N ascending crossovers produce N+1 bands: `cbe-1..7`, `sf-1..7`. Band 0 always present. Bypass→`ce-N=false`, **never `bm-N` or band removal**. Per-band `al/at/rt/cr/kn/mk/ht-N` mirror dynamics units. All solos/mutes false |
| LSP limiter / `limiter-stereo` | threshold→`th` linear; lookahead/attack/release→`lk/at/rt` ms; link→`slink` percent, initially 100; `boost=false`, `ovs=0`, `mode=0` (Herm Thin). Hidden ALR preserved. No RF ceiling claim |

AGC native grow/fall amounts are separate from their times. Saved amount indexes
`0..12` map to `[0.1,0.5,1,3,6,9,10,12,15,18,20,21,24]` dB; default index 7 (12 dB)
and times come from installed LSP metadata, whose TTL specifies milliseconds.
Maximum-gain limitation starts enabled with the plugin's saved 36 dB value;
this is a cap, not a recommended broadcast target. Short-time Grow's speed remains
saved while Off. Enabling it can produce large transient boosts; a limiter remains
a separate stage. No leveller is substituted for final RF measurement.

Four multiband bands start at 120/1000/5000 Hz boundaries. Add/remove crossovers to
reach 1–8 bands; all eight band settings remain saved when inactive. Wideband and
Multiband may both run. Per-band bypass leaves crossover audio present.

## Blocked / limited implementations

* **Windowed Sinc is blocked**, even though its factory is installed. Standalone
  `audiowsinclimit mode=0 cutoff=15000 length=101 window=0 low-latency=false`
  tests initially passed, then intermittently produced non-finite/huge output
  (e.g. peak `1.04e34`) with 96,000-frame input buffers. The actual adapter also
  caught a `5.66e35` peak with a 48,000-frame buffer. This does **not** establish
  the internal plugin root cause or prove that all normal decoder block sizes fail.
  It is sufficient to withhold operational selection. Settings remain validated
  and retained; selecting it fails explicitly, never substitutes Chebyshev.
* **audioloudnorm:** no factory and no runtime Rust-plugin package candidate in
  the Pi's configured repositories. No adapter, UI knobs or untested 192 kHz island
  is advertised. No repository was added and no system plugin was compiled.
* **LSP advanced modes:** limiter oversampling only Off; Herm Thin only; EQ IIR/RLC(BT)
  only; multiband Classic only. Other native enum values were inspected but are
  not accepted as working options without algorithm/load validation. Actual limiter
  `ovs` enum spans None, Half x2/x3/x4/x6/x8 (16/24 bit), Full equivalents; this
  installed 1.2.21 wrapper has no additional True Peak enum. They are not guessed.
* Not every LV2 sidechain/control port is supported. Unknown fields are rejected,
  not ignored. All **supported** hidden settings survive editor saves.

These are implementation limits, not changes to the approved design.

## Pi environment and measured evidence

Target: Raspberry Pi 3 Model B Plus Rev 1.3, aarch64 Debian 13 (trixie),
GStreamer 1.26.2, PyGObject 3.50.0. Operator installed from existing repositories:
`lsp-plugins-lv2=1.2.21-5`, `lsp-plugins-r3d-glx=1.2.21-5` (no upgrades/removals).
Existing native packages: plugins-good `1.26.2-1+deb13u2`, plugins-bad
`1.26.2-3+rpt3+deb13u2`, base `1.26.2-1+rpt3+deb13u1`, tools `1.26.2-2`.
No application deployment, service restart, production DB change or hardware
command occurred. Tests used a separate `/tmp` workspace and localhost files.

Isolated native results, not RF/audio-quality acceptance:

* Native volume multiplier .5 measured .500000001; left-only inputs remained
  silent on the right through all seven LSP stereo adapters.
* HPF 30 Hz: 10 Hz gain .01234, 1 kHz gain ~1. Chebyshev LPF 15 kHz:
  1 kHz ~1, 20 kHz .02585 (four poles/type I/ripple 0).
* GStreamer and LSP Bell at 1 kHz/+6 dB measured ~1.99526 amplitude gain.
  LSP Notch at 1 kHz measured ~2.94e-6 steady-state gain; removal restores unity.
* Basic hard compressor threshold .5/slope .5 maps .8→.65; expander
  threshold .5/slope 2 maps .4→.3. `rglimiter` .9 input produces ~.832, proving
  threshold ≠ ceiling. LSP limiter .1 threshold measured peak ~.099999 in its test.
* Four/eight-band bypass retained unity RMS within 2%; active compression test
  measured gains .1153/.1296. These measurements don't certify flat phase or every
  crossover/ratio combination.
* Autogain tracked a level step; explicitly enabling Short-time Grow changed its
  recovery (final measured RMS .01767 vs .004565 with the selected slow Grow test).
* Real HTTP WAV decode resampled 44.1 kHz mono to S16LE/48 kHz stereo fan-out.
  Source −6 dB measured peak 4007; Source −6 plus Profile −6 measured 2010
  from an 8000-peak fixture. Audio-only rebuild retained the same output object.
  Cold start ~537 ms; subsequent starts/rebuilds ~47–57 ms; max process RSS ~58 MiB.
  These startup times are **not** steady-state end-to-end programme latency.

### Operator-authorized bounded eight-stream run

Two six-second measurement windows used **eight different 128 kbit/s MP3 files**
(different tones), eight localhost HTTP decoders and eight independent chains.
No shared decoder/source shortcut, mux connection or audio device was used.

| Measurement | AGC + limiter | AGC + LSP EQ + wideband + four-band + limiter |
|---|---:|---:|
| Whole-host CPU, averaged over four cores | 39.7% | 73.2% |
| Per-core CPU | 38.6 / 40.7 / 40.5 / 38.8% | 73.4 / 71.6 / 73.2 / 74.5% |
| Test process CPU (one core = 100%) | 160.5% | 281.9% |
| Process high-water RSS | 169.5 MiB | 298.2 MiB |
| Host available memory at end | 576.9 MiB | 461.8 MiB |
| Start all eight | 1.09 s | 1.80 s |
| Maximum observed PCM delivery gap | 37.6 ms | 58.4 ms |
| Staging queue drops, each of eight Sources | 0 | 0 |
| Rebuild one Source, same output ownership | 134 ms | 303 ms |
| Other seven Sources remain RUNNING | yes | yes |

Each Source delivered approximately 1.15–1.17 MB of PCM per window (expected
192,000 bytes/s). Slight excess reflects buffer boundaries/startup backlog.
RSS is process high-water memory across sequential cases, not isolated per-plugin
allocation. Host CPU includes other existing activity. Queue-drop counts are
**not RP/mux underrun measurements**; delivery gaps are not end-to-end latency.
Fixtures, CPU overhead and the short window do not establish programme-quality,
network-jitter or sustained thermal/load suitability. Limiter oversampling remained
Off, multiband Classic, EQ IIR; expensive modes remain unverified.

## Reproduce safely / deployment prerequisites

Inspect packages and factories first. Install from configured repositories only:

```sh
sudo apt-get install --no-upgrade lsp-plugins-lv2
gst-inspect-1.0 lsp-plug-in-plugins-lv2-autogain-stereo
gst-inspect-1.0 lsp-plug-in-plugins-lv2-mb-compressor-stereo
```

Use an isolated copy of the Python package plus `daemon/tools/` in a temporary
workspace, using an existing Python with GI/Pydantic. Do not update the deployed
checkout to run tests. From that workspace:

```sh
nice -n 15 python dsp_adapter_probe.py
nice -n 15 python dsp_source_probe.py
# Only when operator approves an eight-stream load window:
timeout 65s nice -n 15 python dsp_load_probe.py
```

`dsp_native_probe.py` and `lsp_native_probe.py` retain standalone property/audio
discovery cases (the former includes the known unsafe sinc diagnostic). All output
is to memory or temporary fixtures, never an audio device or mux socket. Backend
property descriptions match installed names, and readback detects clamping on
incompatible plugin versions. Save failure leaves files unchanged; saved/restart
failure remains the existing explicit partial result. Final source negotiation is
still checked by the real pipeline before a Source start reports success.

Eight distinct-stream endurance, live-network jitter/reconnection, total pipeline
latency, expensive optional modes, sinc root cause, and RF/MPX/deviation/pilot-region
measurements remain separate acceptance gates. No synthetic test proves those.

## Automated validation in this implementation

* Full daemon suite: **650 passed, 32 skipped**. Skips: 25 GI/GStreamer tests on
  Windows, five Linux mux integrations, one Linux i2c-dev test, one Windows symlink
  creation privilege test. The existing Starlette/httpx deprecation warning remains.
* Full frontend suite: **164 passed**, eight test files. Typecheck and production
  WebUI build passed. `git diff --check` passed.
* Focused profile/schema/audio-lifecycle run: 58 passed/one platform skip at the
  earlier checkpoint; later added regressions are included in the full counts.
* An intermediate full run caught extra read-only background GET_INFO/GET_STATUS
  polls in the unchanged `info_rejection` lifecycle test. The isolated rerun and
  final serial full run passed; no recovery code or that test was modified.
* Native Pi: standalone property/audio probes, final production-adapter assertions,
  real decoder/gain/fan-out/rebuild test, and the authorized bounded eight-stream
  workload ran. Pytest itself is absent from the Pi venv; the Windows-skipped native
  suite was not claimed as executed on the Pi. The standalone checks are separate.

No RF listening, FM deviation, MPX, firmware or live deployment validation was done.

## Changed files

* Backend: `daemon/models/audio_profile_file.py`, new `daemon/models/dsp.py`,
  `daemon/sources/gst_pipeline.py`, new `daemon/sources/dsp.py`, `daemon/api/routes.py`.
* Backend tests: `test_audio_profile_files.py`, `test_audio_profiles.py`,
  `test_gstreamer.py`, new `test_modular_dsp.py` under `daemon/tests/`.
* Standalone verification: `dsp_native_probe.py`, `lsp_native_probe.py`,
  `dsp_adapter_probe.py`, `dsp_source_probe.py`, `dsp_load_probe.py` under `daemon/tools/`.
* Existing WebUI feature: `profile-api.ts`, `profile-editor.ts`, `profiles.spec.ts`,
  new `dsp-fields.ts` and `dsp-fields.spec.ts` under
  `webui/src/app/features/audio-profiles/`. Removed the now-unused `basic-config.ts`;
  editor defaults/constraints come from the validated backend model.
* Documentation: `README.md`, `docs/audio-profile-files.md`,
  `docs/fmheadend-software-architecture.md`, `docs/webui-spec.md`, `docs/milestone6a.md`,
  this report, and the implementation-status note in the canonical
  `docs/fmheadend-audio-processing.md` (approved design text retained).

The shipped Basic JSON, Source DB schema, source manager/lifecycle, RP firmware,
control protocol, recovery, mux, SPI/audio transport and RDS scheduling are unchanged.
