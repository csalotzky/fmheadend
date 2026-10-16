# Standalone project validation — 2026-10-16

This was a structure/import/path refactor. Firmware C/H/PIO behavior, partition
contents, protocol, timings, CRC and version policy were not changed in this work.
Build identity changes because the CMake input files changed.

## Moves and dependency boundary

- Build, provisioning and artifact scripts now reside in this project's `tools/`.
- Offline partition/provisioning tests now reside in `tests/`.
- Project instructions now reside in this project's `AGENTS.md`.
- CMake and VS Code tasks invoke only these local tools. Provisioning imports its
  adjacent artifact module, reads local partitions.json and uses CMake's Python.
- Bench tools and their tests use `tools/bench_support`, a local snapshot of the
  previously shared codec/client/encoder/configuration definitions. Their behavior
  is retained; no host daemon source is imported. Pydantic 2 is an optional bench
  dependency, not a build/provisioning dependency.
- The host uploader remains a separate application, with its artifact-helper
  import updated to the moved module. No uploader copy is included here.
- README and instructions are self-contained; historical host-document links
  are not required to build, program, debug or test this project.

## Checks performed

1. Copied sources, hidden VS Code settings and local tools into a new system-temp
   directory outside the original repository, using a path containing a space.
   Old generated build directories were omitted for the clean build.
2. Configured/built factory and OTA with Pico SDK 2.3.1, CMake 4.3.4, ARM GCC
   15.2 and independently installed Python 3.12.9. Initial interpreter discovery
   selected the host venv; the clean validation explicitly selected installed
   Python instead. Neither firmware build depends on that host venv.
3. Validated both 63,116-byte binaries and manifests: factory ROM 1.0/non-TBYB,
   OTA ROM 1.1/TBYB, plus the 140-byte partitions.bin and both ELF symbol files.
4. Checked the firmware build graph/cache/compile commands for references to the
   original repository: none. Verified installed OpenOCD scripts, GDB and SVD
   paths, local task entry points, attach-only debug and non-interactive flashing.
5. Ran provisioning `--dry-run` from that external copy: passed, no device access.
6. Built and ran native tests from the external copy: **24/24 CTest passed**
   (15 native C tests, 9 Python checks, including project-boundary checks).
   The native compiler was the existing Zig/Clang tool; its warm compiler cache
   was reused after cold-cache initialization stalled. No host application
   sources or Python modules were used by these CTest checks.
7. Offline provisioning/partition and host uploader compatibility tests:
   **74 passed**, using the existing Python test environment and picotool.
8. Copied the external project again, this time retaining its generated firmware
   build/cache. The normal build script detected relocation and reconfigured;
   both binaries and manifests were byte-identical to the first external build.

Factory SHA-256: `17884324acd5825af675f57850e9deaf822895d5e6877a97813a7e77349c5236`.
OTA SHA-256: `71b10e553937eb46cc1fb709213330b5710fc96b08d21686d0ab9b06aa5a79ce`.

No flash erase, SWD programming, live debug connection, I2C OTA or other hardware
operation was performed. SWD/debug portability was checked through configuration,
paths, symbol artifacts and provisioning preflight, not a live connection. Prior
reset/CRC hardware limitations remain unchanged.
