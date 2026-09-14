# fmheadend Web UI Specification

## 1. Purpose

This document defines the initial UX and visual direction for the `fmheadend` Web UI.

The Web UI is intended to behave like a professional broadcast/headend appliance interface:
dense, clear, operationally focused, and easy to scan at a glance.

The UI must expose the existing backend concepts directly rather than inventing a second product model.

## 2. Technology

- Angular
- TypeScript
- Angular Router
- HttpClient
- Reactive Forms
- Simple feature services around the REST API

Avoid unnecessary frontend infrastructure unless a concrete need appears.

Do not introduce by default:

- NgRx
- SSR
- BFF layer
- WebSockets
- frontend microservices
- elaborate design-system frameworks

Angular Material may be used selectively if useful, but the UI must not look like a default scaffolded Material admin panel.

## 3. Global layout

Desktop-first, responsive layout.

Main structure:

- persistent top header
- persistent or collapsible left navigation
- main content area

The interface should remain usable on tablets and smaller screens, but the primary target is a desktop browser used to operate the headend.

### 3.1 Global header

The global header is visible on every page.

It should show a compact system summary such as:

- RP2354 status
- muxd status
- active TX count, e.g. `6 / 8 TX ON`
- significant global error indication

Global actions:

- `START ALL`
- `STOP ALL`

These are operational controls and should be visually distinct from passive status information.

`STOP ALL` must be easy to find and difficult to confuse with navigation or configuration controls.

The header must not contain eight individual per-modulator controls.

## 4. Navigation / sitemap

Primary navigation:

1. Dashboard
2. Sources
3. Modulators
4. Audio Profiles
5. System

Do not create separate top-level navigation entries for RDS, logs, hardware, status, network, or low-level debug unless the product later proves that one is necessary.

## 5. Dashboard

The Dashboard is the main operational page and should be the default landing page.

Its primary subject is the set of physical modulators.

For an eight-QN board, all eight modulators should fit comfortably on a normal desktop display, preferably as a 4 × 2 grid.

### 5.1 Modulator cards

Each physical modulator gets one card.

The card is primarily read-only.

The only editable control on the Dashboard is the modulator `START / STOP` control.

Do not allow inline editing of frequency, RF level, source assignment, RDS configuration, pre-emphasis, or other configuration values from the Dashboard.

Suggested information hierarchy:

- physical modulator ID (0..7), with no separate name
- TX state
- frequency — visually dominant
- source name
- PS
- PI
- ECC (nullable 8-bit code; integer API, hexadecimal display such as E0 is suitable)
- Fixed AF Method A list (ordered frequencies in kHz; max 25, 87600..107900 on a 100 kHz raster)
- relative RF output level
- stereo / mono
- pre-emphasis
- source/runtime status
- error state, if any

Example conceptual content:

```text
TX 1                                  ON

91.60 MHz
FM4 Internet

PS  FM4
PI  A213

Stereo · 50 µs · 6 dB below maximum
Source RUNNING · PCM OK · RDS OK
```

The exact layout may be designed during implementation.

### 5.2 Status semantics

Status colors should be used only when they communicate operational meaning.

Examples:

- normal / running
- stopped / inactive
- warning / reconnecting
- error

Do not turn every statistic into a colored badge.

Errors should be visible directly on the relevant modulator card without requiring the user to open another page.

Clicking a card may navigate to the corresponding modulator configuration page.

## 6. Sources

Use the existing backend term `Sources`.

Do not call this page `Stream Manager`, because not every source is a network stream.

Supported V1 source types:

- HTTP / HLS
- UDP
- RTP
- ALSA

### 6.1 Source list

The source list should expose useful operational information such as:

- name
- type
- state
- which modulator(s) currently use it
- detected / normalized audio format
- current metadata when available
- edit action
- test action

Example:

```text
FM4 Internet    HTTP    RUNNING    TX1    MP3 → 48k stereo    Artist – Title
Behringer USB   ALSA    IDLE       —      48k stereo          —
```

### 6.2 Source creation/editing

Use a conventional form.

The form changes according to source type.

Examples:

HTTP:
- name
- URL
- optional structured GStreamer overrides
- audio profile

UDP / RTP:
- address / port
- protocol-specific fields
- audio profile

ALSA:
- device
- input format where required
- audio profile

Analog input is configured on the physical Modulator, not in Sources.

### 6.3 Source test

The existing backend source-test functionality should have a first-class UI.

A source should be testable before saving where possible.

HTTP testing may fill a blank Source Name from reliable station identity, but never
overwrites a nonblank name or saves the Source. The URL and Test stream button share
a full-width row; other fields stay below. User-Agent is configurable only through
the existing advanced GStreamer source property. See milestone7b1.md for implemented
probe metadata and fallback semantics.

Useful result presentation:

- connection success/failure
- reliable station identity when available (separate from now-playing title)
- detected codec
- approximate input bitrate when reported by GStreamer
- detected input sample rate
- channel count
- normalized output caps
- stream metadata when available
- concise error message

Example:

```text
Connected

MPEG-1 Layer 3
44.1 kHz stereo input
Normalized to 48 kHz / S16LE / stereo
```

## 7. Modulators

This page contains persistent configuration for physical modulators.

Modulators are fixed hardware resources with IDs `0..7`; the UI must not present create/delete operations for them.

The Modulator editor is one page with RF, Audio and RDS sections in that order, without tabs.

Recommended sections:

- RF
- Audio
- RDS

### 7.1 RF

Fields:

- frequency
- relative RF output: Maximum, then -1.5 dB steps through -22.5 dB
- stereo / mono
- pre-emphasis: 50 µs / 75 µs

Frequency is required before TX can be started.

Do not invent a default frequency.

### 7.2 Audio

Fields:

- Digital source / Analog input (`audio_mode`), with reusable source selector only for DIGITAL
- Gain control: Automatic / Manual
- Analog input gain in dB, visible only for ANALOG
- Digital trim: 0 / +1 dB
- Soft clipping: On / Off

AUTO keeps both manual gains visible where applicable but disabled, preserving stored values.
Soft clipping is independent. No raw QN gain mapping is exposed. Analog saturation is a
current read-only warning; DIGITAL/unavailable observations do not imply input health.
ANALOG selects the dedicated jack and clears input_source_id. Audio Profiles belong to Sources.

### 7.3 RDS

RDS belongs to the modulator and should not be a separate top-level page.

Fields:

- RDS enabled
- PI
- ECC (nullable 8-bit code; integer API, hexadecimal display such as E0 is suitable)
- Fixed AF Method A list (ordered frequencies in kHz; max 25, 87600..107900 on a 100 kHz raster)
- PTY 0..29 with European labels only
- TP
- PS list
- fallback RadioText
- Use source metadata for RadioText

PS behavior:

- one PS item = static PS
- multiple PS items = dynamic PS loop
- each dynamic PS item may have a duration
- PS length is limited to 8 characters

Runtime RadioText override and metadata-derived RadioText may be displayed where useful, but the persistent fallback RT should remain clearly distinguishable from runtime content.

Manual Fill from source sets PI to FF00 + physical TX ID, replaces PS with one
static entry, and sets fallback RT. It uses the current conservative RDS conversion,
with 8/64-character limits, and confirms before replacing a carousel. Nothing follows
source changes automatically; values remain unsaved until Save. There is no Station entity.

Save/Cancel use a common action area and protect dirty navigation. Active-TX PATCH
returns 409 without saving/stopping/restarting; lifecycle remains explicit.
See [M7B2](milestone7b2.md) for implementation and verification.

## 8. Audio Profiles

The Audio Profiles list/editor uses Name, Gain and Processor only. NONE is shown
as "None (gain only)"; GST_BASIC as "GStreamer Basic", with an explicit gain-only
limitation until additional DSP exists. No separate bypass switch or placeholder
DSP controls are exposed. Gain applies for both processors. Used-by counts refer
to InputSources. All edits to profiles used by running transmitters and referenced-profile deletes
surface backend conflicts; no source restart is hidden in an edit action.

See [M7B3](milestone7b3.md) for implemented capabilities and safety semantics.

## 10. System

V1 System is a read-only overview: backend version/status and selected media
backend, RP connection/protocol/firmware/hardware, mux status/transport, current
region plan and database schema. Diagnostics use existing counters and current
errors; latched codes are marked historical. Missing values are unavailable.
No CPU/temperature claims, raw register controls, live logs or administrative
commands are exposed. Partial dependency failure keeps the page usable.

Region is a startup-only frequency selection plan returned by the backend;
frequency choices use its integer min/max/step. Existing out-of-plan frequencies
remain explicit and unchanged. RDS AF is independent. See [M7B5](milestone7b5.md)
for the central plan contract, production hosting and final sitemap.

## 11. Visual direction

The implementation may design the detailed appearance, but it should follow these constraints:

- professional broadcast/headend appliance feel
- clean but information-dense
- restrained typography
- strong information hierarchy
- frequencies should be especially easy to scan
- status colors only where semantically useful
- compact forms and tables
- sensible whitespace, but no oversized empty layouts
- no decorative gradients
- no glassmorphism
- no consumer streaming-app aesthetic
- no gratuitous animation
- no oversized hero cards
- no generic AI-generated admin-dashboard look

The design should make eight transmitters feel like eight channels of one appliance, not eight unrelated widgets.

## 12. Interaction principles

- Operational status should be visible without drilling down.
- Configuration and operation should remain clearly separated.
- Dashboard controls should be deliberately limited.
- Errors should appear close to the object that caused them.
- Destructive or high-impact actions should be visually distinct.
- Backend terminology should remain consistent in the UI.
- Avoid exposing implementation details that users do not need.
- Prefer straightforward forms and tables over clever interaction patterns.

## 13. Runtime refresh

V1 may use periodic REST polling.

A roughly 5-second refresh interval is acceptable for normal dashboard/status data unless later testing shows that a different value is preferable.

WebSockets are not required for V1.

User-triggered operations such as START / STOP should update the relevant UI immediately and then reconcile with backend runtime status.

## 14. Non-goals for the initial Web UI

The initial Web UI does not require:

- authentication
- role management
- multi-user workflows
- WebSockets
- mobile-first design
- advanced frontend state management
- server-side rendering
- network management
- direct Linux hardware configuration
- RP2354 firmware update UI
- arbitrary QN register control in normal views

## 15. Implementation freedom

The detailed visual design is intentionally not frozen before implementation.

The first implementation should follow this UX structure and visual direction, then be refined after it can be evaluated in a real browser.

Layout, spacing, typography, card design, form arrangement, icons, and responsive behavior may be adjusted iteratively without changing the underlying information architecture.

## Shared layout conventions

Routed configuration pages use the global `page-content` host class: a centered
content container, never centered text. The standard maximum width is 1200px;
editors select `editor-narrow` (850px, Sources/Audio Profiles) or `editor-wide`
(1120px, Modulators). Containers remain full-width within smaller viewports.

List headers use `page-header`: title left and primary action right within the
bounded content area, wrapping naturally at narrow widths. Editors use bottom
`editor-actions` with Save then Cancel. Cancel uses normal routing and existing
dirty-navigation protection; a separate top Back link is unnecessary.

Shared structural CSS belongs in `styles.css`; do not duplicate host centering,
header alignment or action-row layout per feature. Keep genuinely specific form
and channel styling local. The System page follows these same
container, header and editor-action conventions, selecting the appropriate width.
