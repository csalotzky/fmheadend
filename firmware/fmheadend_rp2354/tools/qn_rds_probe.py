"""TX0-only QN RDS polling experiment on unchanged M8.6A firmware.

handshake performs real register writes with RF OFF (not an offline simulation).
over-air explicitly enables RF and repeats four Linux-encoded PS segments.
No FIFO use, consumer, interrupt handling, recovery or acceptance assumption.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from daemon.rds.encoder import group_0a
from daemon.rp2354.client import TxConfig
from daemon.rp2354.linux_client import Rp2354Client

SYSTEM1, SYSTEM2, I2S, GAIN_TXPLT = 0x00, 0x01, 0x07, 0x0F
RDSD0, STATUS2 = 0x10, 0x1B
RDSTXRDY, RDS_RXTXUPD = 0x04, 0x80
PS = b"FMTEST  "


def groups():
    return [group_0a(0x1234, 0, False, PS, segment, stereo=True, af=())
            for segment in range(4)]


def group_bytes(group, order):
    if order not in ("low-first", "high-first"):
        raise ValueError("Select an explicit byte-order test case")
    return b"".join(word.to_bytes(2, "little" if order == "low-first" else "big")
                    for word in group)


class Probe:
    def __init__(self, rp, poll_seconds, poll_interval_ms, *, clock=time.monotonic,
                 sleep=time.sleep, emit=print):
        self.rp, self.clock, self.sleep, self.emit = rp, clock, sleep, emit
        self.poll_seconds = poll_seconds
        self.interval = poll_interval_ms / 1000
        self.origin = clock()

    def log(self, text):
        self.emit(f"t={self.clock()-self.origin:8.3f}s {text}", flush=True)

    def read(self, reg):
        start = self.clock() - self.origin
        value = self.rp.qn_read_register(0, reg)
        return value, f"host call=[{start:.3f},{self.clock()-self.origin:.3f}]s"

    def observe(self, group, order, segment):
        data = group_bytes(group, order)
        self.log(f"CASE {order} segment={segment} words=" +
                 " ".join(f"{word:04X}" for word in group))
        self.log("RDSD0..7 bytes=" + data.hex(" ").upper())
        system2, stamp = self.read(SYSTEM2)
        self.log(f"1 SYSTEM2 before data=0x{system2:02X} {stamp}")
        baseline, stamp = self.read(STATUS2)
        self.log(f"2 STATUS2 baseline=0x{baseline:02X} bit7={baseline >> 7} {stamp}")
        # Never copy SWRST/RECAL into a probe toggle. Refuse, rather than mask
        # unrelated bits or silently run through an unexpected device state.
        if system2 & 0xC0:
            raise RuntimeError("SYSTEM2 SWRST/RECAL asserted; stop and inspect")
        for index, byte in enumerate(data):
            start = self.clock() - self.origin
            self.rp.qn_write_register(0, RDSD0 + index, byte)
            end = self.clock() - self.origin
            if index == 7:
                # First next bus command, with no inserted sleep or log output.
                after_data, stamp = self.read(STATUS2)
            self.log(f"3 WRITE RDSD{index} reg=0x{RDSD0+index:02X} value=0x{byte:02X} "
                     f"host call=[{start:.3f},{end:.3f}]s")
        self.log(f"4 STATUS2 immediately after RDSD7=0x{after_data:02X} "
                 f"bit7={after_data >> 7} {stamp}")
        before_toggle, stamp = self.read(SYSTEM2)
        self.log(f"5 SYSTEM2 before toggle=0x{before_toggle:02X} {stamp}")
        if before_toggle != system2:
            raise RuntimeError("SYSTEM2 changed unexpectedly before toggle; stop and inspect")
        toggled = before_toggle ^ RDSTXRDY
        toggle_start = self.clock()
        self.rp.qn_write_register(0, SYSTEM2, toggled)
        self.log(f"6 WRITE SYSTEM2 0x{before_toggle:02X}->0x{toggled:02X} "
                 "XOR=0x04 only; no acceptance assumed")
        readback, stamp = self.read(SYSTEM2)
        self.log(f"7 SYSTEM2 readback=0x{readback:02X} {stamp}")
        if readback != toggled:
            raise RuntimeError("SYSTEM2 readback mismatch; no repeat or recovery")

        # Full bounded observation window, even if a change appears immediately.
        # Log each value transition (including return to a previously seen value).
        deadline = self.clock() + self.poll_seconds
        previous = after_data
        values = [after_data]
        transitions = []
        polls = 0
        while self.clock() < deadline:
            value, stamp = self.read(STATUS2)
            polls += 1
            elapsed = self.clock() - toggle_start
            if polls == 1 or value != previous:
                self.log(f"8 STATUS2 poll=0x{value:02X} bit7={value >> 7} "
                         f"observed +{elapsed:.3f}s from toggle-call start {stamp}")
            if (value ^ previous) & RDS_RXTXUPD:
                transitions.append(elapsed)
            if value not in values:
                values.append(value)
            previous = value
            remaining = deadline - self.clock()
            if remaining > 0:
                self.sleep(min(self.interval, remaining))
        changed_before = bool((after_data ^ baseline) & RDS_RXTXUPD)
        self.log(f"9 OBSERVATION bit7 changed baseline->after-RDSD7={changed_before}; "
                 f"post-toggle observed bit7 transition times={transitions}; "
                 f"distinct poll values={[f'0x{v:02X}' for v in values]}; polls={polls}")
        self.log("No acceptance verdict. Changes between reads may be missed; times are host observations.")
        return changed_before or bool(transitions)


def run(rp, args, *, clock=time.monotonic, sleep=time.sleep, emit=print):
    probe = Probe(rp, args.poll_seconds, args.poll_interval_ms,
                  clock=clock, sleep=sleep, emit=emit)
    touched = False
    try:
        info = rp.get_info()
        probe.log(f"GET_INFO {info}")
        if info["rp_firmware_version"] != "0.8.6a":
            raise RuntimeError("Use unchanged M8.6A firmware 0.8.6a; no concurrent RDS consumer")
        status = rp.get_status()
        if status["state"] != "OK" or len(status["modulators"]) != 8 or any(
                ch["tx_state"] != "OFF" for ch in status["modulators"]):
            raise RuntimeError("Start with all eight TXs OFF and no active hardware fault")
        touched = True
        rp.set_tx_enabled(0, False)
        rp.set_tx_config(0, TxConfig(frequency_khz=87500, rf_attenuation_db=22.5,
            stereo=True, preemphasis_us=50, audio_mode="DIGITAL", rds_enabled=True,
            gain_mode="AUTO", analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False))
        system1, _ = probe.read(SYSTEM1)
        i2s, _ = probe.read(I2S)
        pilot, _ = probe.read(GAIN_TXPLT)
        probe.log(f"SETUP TX0 87500 kHz attenuation=22.5 dB DIGITAL RDS=ON; "
                  f"SYSTEM1=0x{system1:02X} I2S=0x{i2s:02X} GAIN_TXPLT=0x{pilot:02X}")
        if system1 != 0x07 or i2s != 0x71 or pilot & 0x02:
            raise RuntimeError("Unexpected setup or RDS interrupt enable set; reset RP and inspect")
        probe.log("GPIO26 unused. Raw commands retain the existing 100 ms client wait. "
                  "No FIFO access; raw writes invalidate configuration eligibility.")
        if args.mode == "over-air":
            # Raw writes invalidate configured state; enable only here, BEFORE
            # the first one. Never bypass production enable validation via raw SYSTEM1.
            rp.set_tx_enabled(0, True)
            actual, _ = probe.read(SYSTEM1)
            if actual != 0x47:
                raise RuntimeError(f"RF ON readback unexpected: SYSTEM1=0x{actual:02X}")
            probe.log("RF ON: TX0 only, 87.5 MHz; observe PI=1234 PS='FMTEST  ' on receiver")
        else:
            probe.log("HANDSHAKE: RF remains OFF; this is a real register experiment, not offline dry-run")

        sequence = groups()
        stop = clock() + args.seconds
        n = 0
        while True:
            changed = probe.observe(sequence[n % 4], args.byte_order, n % 4)
            n += 1
            if args.mode == "handshake" or clock() >= stop:
                break
            if not changed:
                probe.log("STOP repetition: no observed bit7 transition; capture this trace before further testing")
                break
        probe.log(f"Probe finished: {n} group experiment(s); byte order and on-air decoding require operator review")
    finally:
        try:
            if touched:
                try:
                    rp.set_tx_enabled(0, False)
                    value, _ = probe.read(SYSTEM1)
                    if value & 0x40:
                        raise RuntimeError("SYSTEM1 still reports TXREQ=1")
                    probe.log(f"FINAL RF OFF verified: SYSTEM1=0x{value:02X}")
                except Exception:
                    probe.log("FINAL RF OFF NOT VERIFIED: stop bench RF manually; no automatic recovery attempted")
                    raise
        finally:
            rp.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/i2c-1")
    parser.add_argument("--address", required=True, type=lambda s: int(s, 0))
    parser.add_argument("--byte-order", required=True, choices=("low-first", "high-first"),
                        help="Explicit, separate byte-order experiment; reset RP between cases")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--poll-interval-ms", type=int, default=25,
                        help="Extra interval; each existing raw read also waits 100 ms")
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("handshake", help="One segment-0 group, RF OFF")
    air = commands.add_parser("over-air", help="Explicit RF ON, bounded four-segment repetition")
    air.add_argument("--seconds", type=int, default=60,
                     help="Repetition window 5..120 s; finish current observation then RF OFF")
    args = parser.parse_args()
    if not 0.2 <= args.poll_seconds <= 5 or not 0 <= args.poll_interval_ms <= 500:
        parser.error("poll-seconds must be 0.2..5 and poll-interval-ms 0..500")
    if args.mode == "handshake":
        args.seconds = 0
    elif not 5 <= args.seconds <= 120:
        parser.error("seconds must be 5..120")
    try:
        run(Rp2354Client(args.device, args.address), args)
    except KeyboardInterrupt:
        print("Probe interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"PROBE STOPPED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
