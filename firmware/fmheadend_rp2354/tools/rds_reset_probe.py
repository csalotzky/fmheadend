"""Observe a candidate RDSEN-only reset after a UART-confirmed TX0 ACK timeout.

No automatic recovery claim. Requires RF already ON, ANALOG input and an RDS
fault; never configures/enables RF. Raw writes invalidate production configuration,
so use ANALOG to avoid the raw-diagnostic DIGITAL/SINE source-muting side effect.
Stop other control clients. Always attempts RF OFF once the experiment starts.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_support.linux_client import Rp2354Client
from bench_support.encoder import group_0a


def probe(rp, baseline, *, clock=time.monotonic, sleep=time.sleep):
    start = clock()

    def log(text):
        print(f"{clock():.6f} +{clock()-start:.6f}s {text}", flush=True)

    def read(reg):
        value = rp.qn_read_register(0, reg)
        log(f"READ {reg:02X}={value:02X} (Pi call completion)")
        return value

    def write(reg, value):
        log(f"WRITE {reg:02X}={value:02X} begin")
        rp.qn_write_register(0, reg, value)
        log(f"WRITE {reg:02X} completed")

    def observe(label, seconds):
        end = clock() + seconds
        first = last = read(0x1b)
        changes = []
        log(f"{label}: first STATUS2={first:02X}")
        while clock() < end:
            value = rp.qn_read_register(0, 0x1b)
            if value != last:
                log(f"{label}: STATUS2 {last:02X}->{value:02X}")
                changes.append((last, value))
                last = value
            sleep(.01)
        return first, last, changes

    info = rp.get_info()
    log(f"GET_INFO {info}")
    if info["rp_firmware_version"] != "0.8.6b":
        raise RuntimeError("Requires 0.8.6b; do not hide first-call protocol errors")
    status = rp.get_status()
    if status["modulators"][0]["tx_state"] != "ERROR":
        raise RuntimeError("Need an existing UART-confirmed TX0 ACK timeout, not a healthy handoff")
    if any(c["tx_state"] != "OFF" for c in status["modulators"][1:]):
        raise RuntimeError("Use TX0 only; other transmitters must be OFF")
    system1 = read(0)
    if system1 & 0x46 != 0x42:
        raise RuntimeError("Requires TXREQ=1, RDSEN=1, ANALOG input; configure that bench before inducing timeout")

    try:
        system2 = read(1)
        initial = read(0x1b)
        if (initial ^ baseline) & 0x80:
            raise RuntimeError("Old ACK already changed: this is not an unresolved timeout trial")
        log("CANDIDATE only: RDSEN 1->0->1 with TXREQ/input preserved; no production adoption")
        write(0, system1 & ~2)
        if read(0) != (system1 & ~2):
            raise RuntimeError("RDS-OFF SYSTEM1 readback mismatch")
        off = observe("RDS disabled", .5)
        write(0, system1)
        if read(0) != system1 or read(1) != system2:
            raise RuntimeError("Control bits changed unexpectedly")
        restored = observe("RDS re-enabled; no new handoff", 2.)
        log(f"RESET OBSERVATIONS baseline={baseline:02X} initial={initial:02X} "
            f"off={off!r} restored={restored!r}")
        log("No transition during a finite window is NOT proof that an old ACK was cancelled.")

        # Separate, explicitly labelled new handoff observation. Never interpret
        # its toggle as proof of cancellation of the earlier pending handoff.
        new_baseline = read(0x1b)
        words = group_0a(0x1234, 0, False, b"FMTEST  ", 0)
        for reg, value in enumerate(b''.join(w.to_bytes(2, 'big') for w in words), 0x10):
            write(reg, value)
        before_toggle = read(0x1b)
        sys2 = read(1)
        if sys2 & 0xc0:
            raise RuntimeError("Reset/recalibrate bits unexpectedly asserted")
        write(1, sys2 ^ 4)
        if read(1) != (sys2 ^ 4):
            raise RuntimeError("RDSTXRDY readback mismatch")
        after = observe("ONE new handoff; attribution unproven", 1.)
        log(f"NEW OBSERVATIONS baseline={new_baseline:02X} before_toggle={before_toggle:02X} after={after!r}")
        if read(0) != system1:
            raise RuntimeError("RF/input/RDS control bits changed")
        log("Capture RF/analog audio continuity externally. Review trace before any recovery implementation.")
    finally:
        try:
            rp.set_tx_enabled(0, False)
            if read(0) & 0x40:
                raise RuntimeError("TXREQ remains ON")
            rp.clear_rds(0)
            log("TXREQ OFF verified; reset/reconfigure before subsequent use")
        except Exception:
            log("RF OFF NOT VERIFIED: disable bench RF physically")
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/i2c-1")
    parser.add_argument("--address", required=True, type=lambda s: int(s, 0))
    parser.add_argument("--pending-baseline", required=True, type=lambda s: int(s, 0),
                        help="STATUS2 baseline from the existing timeout's UART trace")
    parser.add_argument("--confirm-rds-reset-trial", action="store_true")
    args = parser.parse_args()
    if not args.confirm_rds_reset_trial or not 0 <= args.pending_baseline <= 255:
        parser.error("Require --confirm-rds-reset-trial and a byte-valued --pending-baseline")
    rp = Rp2354Client(args.device, args.address)
    try:
        probe(rp, args.pending_baseline)
    finally:
        rp.close()


if __name__ == "__main__":
    main()
