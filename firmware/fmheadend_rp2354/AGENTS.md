# RP2354 development and hardware validation

Read README.md first. This is a self-contained firmware project. Keep the V1 protocol,
two-stage READ, bus speed, RF/audio behavior and native ROM A/B + TBYB design.

- Prefer focused tests, firmware builds and relevant real hardware validation;
  mocks are not proof of hardware behavior. Report each category separately.
- Before an agent-initiated SWD programming attempt, ask the user to place the RP2354 in
  BOOTSEL and wait for explicit acknowledgement. Never retry hardware connections
  automatically when BOOTSEL has not been confirmed. User-invoked Flash tasks
  are non-interactive: never add a confirmation prompt to them. Use the factory provisioning
  workflow, including complete flash erase and byte-exact readback verification.
- With task authorization, use the separate OTA uploader over the Pi. Check bus
  ownership first; never stop another service or change daemon configuration
  without authorization. Transfer only needed firmware/tool files.
- Keep CRC checking enabled. Log failed transactions, retries and reconciliation;
  eventual recovery must not be reported as error-free communication.
- Compile must never flash. Use `tools/rp2354_build.ps1`; outputs live under
  `build/{factory,ota}` plus `build/partitions.bin`.
- Do not load a native linked ELF/UF2 directly at 0x10000000: that address contains
  the partition table. VS Code Flash delegates to the provisioning task; debugging
  attaches without loading. Preserve these safeguards when editing VS Code files.
