"""M8.6B explicit RF bench test: Linux 0A encoder -> RP FIFO -> polling QN.

This finite test repeats PS groups only; it does not replace the M5 scheduler.
Keep other host-control clients stopped. No raw QN writes or settling sleeps.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from daemon.rp2354.client import TxConfig
from daemon.rp2354.linux_client import Rp2354Client
from daemon.rds.encoder import group_0a


def exercise(rp, tx, *, clock=time.monotonic, sleep=time.sleep):
    info = rp.get_info()  # Do not hide/retry the separate first-call status-128 issue.
    print("GET_INFO:", info, flush=True)
    if info["rp_firmware_version"] != "0.8.6b":
        raise RuntimeError("Requires production M8.6B firmware 0.8.6b")
    initial = rp.get_status()
    if initial["state"] != "OK" or any(c["tx_state"] != "OFF" for c in initial["modulators"]):
        raise RuntimeError("Expected a healthy bench with all TXs OFF")
    pi, ps = (0x1234, b"FMTEST  ") if tx == 0 else (0x5678, b"TEST2   ")
    groups = [group_0a(pi, 0, False, ps, segment) for segment in range(4)]
    sequence = 0

    def refill():
        nonlocal sequence
        free = rp.get_rds_fifo_free()[tx]
        count = max(0, 23 - (64 - free))  # About two seconds, below 31/64 limits.
        if count:
            rp.push_rds(tx, [groups[(sequence + n) % 4] for n in range(count)])
            sequence += count  # Advance only after the FIFO ACK.
        return free

    def feed(seconds):
        end = clock() + seconds
        report = clock()
        while clock() < end:
            free = refill()
            if clock() >= report:
                print(f"TX{tx} feeding: free={free} queued_by_tool={sequence}", flush=True)
                report = clock() + 5
            sleep(.1)

    def status():
        snapshot = rp.get_status()
        print("GET_STATUS:", snapshot, flush=True)
        channel = snapshot["modulators"][tx]
        if snapshot["state"] != "OK" or channel["tx_state"] != "ON":
            raise RuntimeError("Active fault / TX no longer ON; retain UART trace")
        return channel

    config = TxConfig(frequency_khz=87500, rf_attenuation_db=22.5, stereo=True,
        preemphasis_us=50, audio_mode="SINE", rds_enabled=True, gain_mode="AUTO",
        analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False)
    try:
        rp.set_tx_config(tx, config)
        rp.clear_rds(tx)
        refill()  # Queue while OFF; no consumer or artificial underrun yet.
        rp.set_tx_enabled(tx, True)
        print(f"TX{tx} RF ON at 87.5 MHz; PI={pi:04X}, PS={ps!r}; listen for 1 kHz.", flush=True)
        feed(60)
        before = status()["rds_underrun_count"]
        print("Stop refill for 3 s; RF stays ON, expect one starvation episode.", flush=True)
        sleep(3)
        drained = status()
        if drained["rds_fifo_free"] != 64 or drained["rds_underrun_count"] != before + 1:
            raise RuntimeError("FIFO did not drain with exactly one underrun")
        sleep(1)
        if status()["rds_underrun_count"] != before + 1:
            raise RuntimeError("Empty FIFO counted repeatedly")
        print("Resume refill for 15 s without RF restart.", flush=True)
        feed(15)
        recovered = status()
        if recovered["rds_fifo_free"] == 64 or recovered["rds_underrun_count"] != before + 1:
            raise RuntimeError("RDS did not recover cleanly")
        print("FIFO/status checks passed. Record receiver PI/PS separately; this is not RF proof.")
    finally:
        try:
            rp.set_tx_enabled(tx, False)
            # SYSTEM1 readback proves TXREQ clear even if a latched RDS fault
            # makes the high-level channel status ERROR. It is not an RF meter.
            if rp.qn_read_register(tx, 0) & 0x40:
                raise RuntimeError("TXREQ remains set")
            rp.clear_rds(tx)
            print("TXREQ OFF verified; check carrier absence and LED on the bench.", flush=True)
        except Exception:
            print("RF OFF NOT VERIFIED: disable bench RF physically; retain error/UART trace.", flush=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/i2c-1")
    parser.add_argument("--address", required=True, type=lambda s: int(s, 0))
    parser.add_argument("--tx", type=int, choices=(0, 1), default=0)
    parser.add_argument("--rf-on", action="store_true", help="Explicitly authorize this finite RF bench run")
    args = parser.parse_args()
    if not args.rf_on:
        parser.error("--rf-on is required; use the terminated RF bench setup")
    rp = Rp2354Client(args.device, args.address)
    try:
        exercise(rp, args.tx)
    finally:
        rp.close()


if __name__ == "__main__":
    main()
