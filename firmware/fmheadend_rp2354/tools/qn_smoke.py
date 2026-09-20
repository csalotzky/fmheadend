"""M8.3 conducted-bench test, using the existing Linux RP client (no audio needed)."""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from daemon.rp2354.linux_client import Rp2354Client
from daemon.rp2354.client import TxConfig
from daemon.rp2354.protocol import InvalidArgument


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/i2c-1")
    parser.add_argument("--address", required=True, type=lambda s: int(s, 0))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("cid", help="Read CID2 from all eight QNs; never enable RF")
    cycle = commands.add_parser("cycle", help="Configure one TX, then RF OFF/ON/OFF")
    cycle.add_argument("--tx", type=int, choices=range(8), default=0)
    cycle.add_argument("--frequency-khz", type=int, required=True)
    cycle.add_argument("--attenuation-db", type=float, required=True)
    cycle.add_argument("--mode", choices=("DIGITAL", "ANALOG", "SINE"), default="DIGITAL")
    cycle.add_argument("--hold-seconds", type=float, default=10)
    args = parser.parse_args()
    if args.command == "cycle" and not 0 < args.hold_seconds <= 60:
        parser.error("--hold-seconds must be >0 and <=60")
    rp = Rp2354Client(args.device, args.address)
    try:
        info = rp.get_info()
        print("GET_INFO:", info, flush=True)
        if info["rp_firmware_version"] != "0.8.3":
            raise RuntimeError("This smoke requires M8.3 firmware 0.8.3")
        for tx in range(8):
            cid = rp.qn_read_register(tx, 0x06)
            print(f"TX{tx} QN 0x{0x21 + tx:02X}: CID2=0x{cid:02X}", flush=True)
            if cid >> 4 != 1:
                raise RuntimeError(f"Unexpected CID2 for TX{tx}")
        if args.command == "cid":
            print("Eight end-to-end CID2 reads passed; no RF command sent.")
            return

        def status(expected):
            snapshot = rp.get_status()
            print("GET_STATUS:", snapshot, flush=True)
            if snapshot["state"] != "OK":
                raise RuntimeError("Active hardware fault")
            for ch in snapshot["modulators"]:
                want = expected if ch["id"] == args.tx else "OFF"
                if ch["tx_state"] != want:
                    raise RuntimeError(f"TX{ch['id']}: expected {want}, got {ch['tx_state']}")
            return snapshot

        status("OFF")  # Refuse to disturb a bench with other active transmitters.
        config = TxConfig(frequency_khz=args.frequency_khz,
            rf_attenuation_db=args.attenuation_db, stereo=True, preemphasis_us=50,
            audio_mode=args.mode, rds_enabled=False, gain_mode="AUTO",
            analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False)
        try:
            rp.set_tx_enabled(args.tx, False)
            # Invalid tuning must reject without applying a partial configuration.
            try:
                rp.set_tx_config(args.tx, config.model_copy(update={"frequency_khz": 98101}))
            except InvalidArgument:
                print("Invalid 98101 kHz request correctly rejected.", flush=True)
            else:
                raise RuntimeError("Firmware accepted an unrepresentable frequency")
            rp.set_tx_config(args.tx, config)
            snapshot = status("OFF")
            if snapshot["modulators"][args.tx]["rf_attenuation_db"] != args.attenuation_db:
                raise RuntimeError("Attenuation readback differs from configuration")
            print("Configured, RF OFF. Verify no carrier and LED OFF.", flush=True)
            time.sleep(args.hold_seconds)
            rp.set_tx_enabled(args.tx, True)
            status("ON")
            print(f"TX{args.tx} RF ON: measure {args.frequency_khz} kHz carrier; LED ON. "
                  "No digital audio is generated.", flush=True)
            time.sleep(args.hold_seconds)
        finally:
            # Best effort on exceptions/Ctrl-C; an I2C failure cannot prove RF OFF.
            rp.set_tx_enabled(args.tx, False)
        status("OFF")
        print("RF OFF command verified. Check carrier absence and LED OFF physically.")
    finally:
        rp.close()


if __name__ == "__main__":
    main()
