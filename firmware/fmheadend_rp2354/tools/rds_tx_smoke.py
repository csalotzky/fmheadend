"""M8.6B explicit RF bench test: Linux 0A encoder -> RP FIFO -> polling QN.

This finite test repeats PS groups only; it does not replace the M5 scheduler.
Keep other host-control clients stopped. Optional settling waits are Pi-side
diagnostics only; no raw QN writes or production firmware timing changes.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from daemon.rp2354.client import TxConfig
from daemon.rp2354.linux_client import Rp2354Client
from daemon.rds.encoder import group_0a


def exercise(rp, tx, *, pre_tx_rds_settle_ms=0, post_tx_settle_ms=0,
             prefill_before_tx=True, clock=time.monotonic, sleep=time.sleep):
    if any(type(ms) is not int or ms < 0 for ms in (pre_tx_rds_settle_ms, post_tx_settle_ms)):
        raise ValueError("Settling delays must be nonnegative integer milliseconds")
    if prefill_before_tx and post_tx_settle_ms:
        raise ValueError("Post-TX settling requires --empty-until-feed to keep the FIFO empty")
    started = clock()

    def stamp(label, since):
        now = clock()
        print(f"TRACE TX{tx} monotonic_s={now:.6f} elapsed_ms={(now-started)*1000:.3f} "
              f"interval_ms={(now-since)*1000:.3f}: {label}", flush=True)
        return now

    print(f"DIAGNOSTIC TX{tx}: pre_tx_rds_settle_ms={pre_tx_rds_settle_ms} "
          f"post_tx_settle_ms={post_tx_settle_ms} prefill_before_tx={prefill_before_tx}; "
          f"monotonic_s={started:.6f}", flush=True)
    print("TRACE times are Pi-side call boundaries. Existing client waits remain: "
          "SET_TX_CONFIG=1000 ms, SET_TX_ENABLED=150 ms; selected delays are additional.", flush=True)
    print(f"First firmware handoff timestamp is not available in V1 status. Capture existing UART "
          f"'RDS TX{tx} submitted' / 'accepted' / 'FAULT' lines separately; "
          "Pi monotonic time is not the RP clock.", flush=True)
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
    tx_done = post_done = None

    def refill():
        nonlocal sequence
        free = rp.get_rds_fifo_free()[tx]
        count = max(0, 23 - (64 - free))  # About two seconds, below 31/64 limits.
        if count:
            if not sequence:
                stamp("first RDS_PUSH starting (RF OFF prefill)" if tx_done is None else
                      "first RDS_PUSH starting (interval since post-TX settling end)",
                      config_done if tx_done is None else post_done)
            rp.push_rds(tx, [groups[(sequence + n) % 4] for n in range(count)])
            if not sequence:
                stamp("first RDS_PUSH completed (RF OFF prefill)" if tx_done is None else
                      "first RDS_PUSH completed (interval since TX-enable completion)",
                      config_done if tx_done is None else tx_done)
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
        # Clear and verify while RF is OFF, before either experimental boundary.
        # Production smoke preloads the queue; optional empty mode preserves the
        # earlier A/B diagnostic without making firmware depend on host delays.
        rp.clear_rds(tx)
        if rp.get_rds_fifo_free()[tx] != 64:
            raise RuntimeError("Expected empty target FIFO; stop all other RDS/control clients")
        config_started = clock()
        rp.set_tx_config(tx, config)
        config_done = stamp("SET_TX_CONFIG completed (interval is host call duration)", config_started)
        if pre_tx_rds_settle_ms:
            sleep(pre_tx_rds_settle_ms / 1000)
        stamp("pre-TX settling ended (interval since configuration completion)", config_done)
        if prefill_before_tx:
            refill()
            if rp.get_rds_fifo_free()[tx] != 41:
                raise RuntimeError("Expected 23 groups queued with RF OFF")
            print("PREFILLED: 23 groups before TX ON. Verify first UART submission "
                  "tx_age_us >= 150000; Pi timestamps/status cannot prove the gate.", flush=True)
        tx_started = clock()
        rp.set_tx_enabled(tx, True)
        tx_done = stamp("SET_TX_ENABLED completed (interval is host call duration)", tx_started)
        if post_tx_settle_ms:
            sleep(post_tx_settle_ms / 1000)
        post_done = stamp("post-TX settling ended; " +
                          ("FIFO prefilled" if prefill_before_tx else "FIFO still unfilled") +
                          " (interval since TX-enable completion)", tx_done)
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
    parser.add_argument("--pre-tx-rds-settle-ms", type=int, default=0,
                        help="Diagnostic wait after RDS configuration completes, before RF ON (default: 0)")
    parser.add_argument("--post-tx-settle-ms", type=int, default=0,
                        help="Diagnostic wait after RF ON completes, with FIFO empty (default: 0)")
    parser.add_argument("--empty-until-feed", action="store_true",
                        help="Keep FIFO empty across TX ON for the earlier A/B diagnostic; default is prefill")
    args = parser.parse_args()
    if not args.rf_on:
        parser.error("--rf-on is required; use the terminated RF bench setup")
    if args.pre_tx_rds_settle_ms < 0 or args.post_tx_settle_ms < 0:
        parser.error("settling delays must be nonnegative milliseconds")
    if args.post_tx_settle_ms and not args.empty_until_feed:
        parser.error("--post-tx-settle-ms requires --empty-until-feed")
    rp = Rp2354Client(args.device, args.address)
    try:
        exercise(rp, args.tx, pre_tx_rds_settle_ms=args.pre_tx_rds_settle_ms,
                 post_tx_settle_ms=args.post_tx_settle_ms, prefill_before_tx=not args.empty_until_feed)
    finally:
        rp.close()


if __name__ == "__main__":
    main()
