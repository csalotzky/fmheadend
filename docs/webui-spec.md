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
5. Bandscan
6. System

`Bandscan` is conditional and remains pending until the hardware/backend functionality is decided.

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

- modulator identifier / name
- TX state
- frequency — visually dominant
- source name
- PS
- PI
- nominal RF output level
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

Stereo · 50 µs · 96 dBµV
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
- ANALOG

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

ANALOG:
- name
- no GStreamer-specific configuration

### 6.3 Source test

The existing backend source-test functionality should have a first-class UI.

A source should be testable before saving where possible.

Useful result presentation:

- connection success/failure
- detected codec
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

A modulator configuration page should be divided into logical sections or tabs.

Recommended sections:

- RF
- Audio
- RDS

### 7.1 RF

Fields:

- frequency
- nominal RF level
- TX enabled / desired state where appropriate

Frequency is required before TX can be started.

Do not invent a default frequency.

### 7.2 Audio

Fields:

- assigned Source
- stereo / mono
- pre-emphasis: 50 µs / 75 µs
- Audio Profile

Audio mode is derived from the assigned Source type.

Do not expose a duplicate DIGITAL / ANALOG selector if the backend already derives this from the source.

### 7.3 RDS

RDS belongs to the modulator and should not be a separate top-level page.

Fields:

- RDS enabled
- PI
- PTY
- TP
- PS list
- fallback RadioText

PS behavior:

- one PS item = static PS
- multiple PS items = dynamic PS loop
- each dynamic PS item may have a duration
- PS length is limited to 8 characters

Runtime RadioText override and metadata-derived RadioText may be displayed where useful, but the persistent fallback RT should remain clearly distinguishable from runtime content.

## 8. Audio Profiles

Audio Profiles remain a separate page because they are reusable configuration objects.

Initial V1 functionality:

- name
- gain

Future extensions may include:

- EQ
- compressor
- limiter

The UI should not prematurely expose controls for features that the backend does not yet implement.

Sources may reference a reusable Audio Profile.

## 9. Bandscan

Bandscan is a reserved page.

Do not implement a fake or decorative Bandscan page before the hardware/backend behavior is decided.

If implemented later, likely responsibilities include:

- spectrum/band overview
- detected FM carriers
- measured/estimated signal levels
- channel occupancy
- candidate free frequencies
- optional application of a selected frequency to a modulator

Exact behavior depends on the eventual RF measurement implementation.

## 10. System

The System page is primarily status and diagnostics, not a dumping ground for arbitrary low-level configuration.

### 10.1 Normal user-facing system information

Potential read-only information:

- fmheadendd state/version
- muxd state/version
- RP2354 connection state
- RP2354 firmware version
- RP2354 protocol version
- hardware revision
- uptime
- CPU load
- memory use
- temperature
- SPI state
- I2C state
- global error state

### 10.2 Advanced / Diagnostics

Low-level information may be placed in a collapsed or clearly separated Advanced / Diagnostics section.

Examples:

- PCM underrun / overrun counters
- RDS underrun counters
- SPI error counters
- I2C error counters
- mux frame counters
- raw RP2354 information
- QN register debug functionality, if retained

These are diagnostic tools, not primary navigation items.

### 10.3 Configuration policy

Do not expose low-level implementation parameters as normal user settings merely because they are configurable internally.

Examples that should normally remain outside the standard UI:

- I2C clock speed
- I2C bus number
- RP slave address
- SPI clock
- Unix socket paths
- internal polling intervals
- buffer sizes

Such values belong in deployment/config files or advanced development tooling unless a real operational requirement appears.

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
