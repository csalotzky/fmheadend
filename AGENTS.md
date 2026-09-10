# fmheadend — Agent Instructions

## Project specification

Before making architectural or implementation decisions, read:

`docs/fmheadend-software-architecture.md`

This document is the canonical specification for the project.

Its architecture, component boundaries, data model, APIs, runtime behaviour, and milestone definitions MUST be followed unless the user explicitly requests a change.

## Project scope

`fmheadend` is an 8-channel FM headend.

The Linux target is a Raspberry Pi Zero 2 W. Development may take place on more powerful systems, including a Raspberry Pi 4, but implementation decisions MUST remain appropriate for the Pi Zero 2 W.

The Linux software consists primarily of:

* `fmheadendd`: Python main daemon
* `fmheadend-muxd`: C/C++ real-time PCM-to-SPI transport daemon
* a REST API served by `fmheadendd`
* a later Web UI
* an RP2354 connected through SPI and I2C

The RP2354 firmware implementation itself is outside the scope of this repository unless explicitly requested. Its Linux-facing interface is defined by the project specification.

## Architecture rules

Do not redesign the architecture unless explicitly requested.

In particular:

* Do not merge `fmheadendd` and `fmheadend-muxd` into one process.
* Do not perform sample-by-sample audio processing in Python.
* GStreamer is the media ingest, decode, resample, and processing framework.
* Digital audio exchanged with the mux daemon is fixed-format 48 kHz, signed 16-bit little-endian stereo PCM.
* SQLite is the persistent configuration store.
* Runtime state must not be treated as persistent configuration.
* The eight physical modulators are fixed hardware resources and are not dynamically created or deleted.
* RP2354/QN8007B hardware access must be isolated behind the appropriate client/service abstraction.
* REST API handlers must not directly manipulate GStreamer, I2C, SPI, or database internals.
* RDS encoding and scheduling policy belongs on the Linux/Python side.
* The RP2354 RDS interface is treated as a FIFO transport driven by available-buffer-space flow control.
* Missing digital audio must result in silence, not an ever-growing audio queue.

## Keep the implementation simple

Do not introduce infrastructure or dependencies that are not required by the specification.

Do not add, unless explicitly requested:

* Docker
* Redis
* message brokers
* Kubernetes
* authentication
* WebSockets
* separate database servers
* unnecessary microservices
* speculative features

Prefer standard-library functionality and small, well-established dependencies where practical.

## Development workflow

Implement one milestone at a time.

Do not continue into the next milestone unless explicitly requested.

Before implementing a milestone:

1. Read the relevant sections of the architecture specification.
2. Inspect the existing repository.
3. Identify the files and components affected.
4. Keep changes within the requested milestone.

After implementation:

1. Run all relevant tests.
2. Fix failures caused by the implementation.
3. Report what was implemented.
4. Report what remains mocked or intentionally unimplemented.
5. Report the tests that were executed and their results.
6. Explicitly report any deviation from the specification.

Do not silently reinterpret ambiguous requirements. If an implementation decision is not specified and materially affects architecture or external behaviour, surface it to the user.

## Testing

Hardware-facing components should be mockable.

The backend should be testable without:

* an RP2354
* QN8007B hardware
* an SPI device
* an I2C device
* live FM hardware

Unit tests should verify behaviour and component interactions rather than only implementation details.

## Code quality

Prefer clear, maintainable code over clever abstractions.

Avoid premature framework-building and unnecessary abstraction layers.

Use descriptive names and explicit types/models at API and subsystem boundaries.

The project is intended to operate continuously and unattended, so error handling, bounded buffering, recovery, and predictable state transitions are more important than UI sophistication.
