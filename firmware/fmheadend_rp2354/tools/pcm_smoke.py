"""Bounded bench PCM producer for muxd Unix sockets; never accesses SPI or RF.

TXn tone is 1000+500*n Hz at -12 dBFS, identical L/R. The constant pattern is
L=0x1234+n, R=-0x2345-n for easy analyzer identification with RF OFF.
"""
import argparse
from contextlib import ExitStack
import math
from pathlib import Path
import socket
import struct
import time


def pcm_block(tx, first, frames, pattern):
    if pattern == "constant":
        return struct.pack("<hh", 0x1234 + tx, -0x2345 - tx) * frames
    hz = 1000 + 500 * tx
    period = 48000 // math.gcd(48000, hz)
    samples = [round(32767 * 10 ** (-12 / 20) * math.sin(2 * math.pi * hz * n / 48000))
               for n in range(period)]
    return b"".join(struct.pack("<hh", samples[(first + n) % period],
                               samples[(first + n) % period]) for n in range(frames))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--tx", type=int, choices=range(8), action="append", required=True)
    parser.add_argument("--pattern", choices=("tone", "constant"), default="tone")
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 3600:
        parser.error("--seconds must be >0 and <=3600")
    with ExitStack() as stack:
        outputs = {}
        for tx in sorted(set(args.tx)):
            sock = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            sock.settimeout(1)
            sock.connect(str(args.runtime_dir / f"pcm{tx}.sock"))
            outputs[tx] = sock
        first = 0
        deadline = time.monotonic()
        until = deadline + args.seconds
        print(f"PCM {args.pattern} TX{list(outputs)}; Ctrl-C closes producers; muxd continues silence", flush=True)
        while time.monotonic() < until:
            for tx, sock in outputs.items():
                sock.sendall(pcm_block(tx, first, 480, args.pattern))
            first += 480
            deadline += 0.01
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # Do not build an unbounded catch-up burst after a paused process.
                deadline = time.monotonic()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
