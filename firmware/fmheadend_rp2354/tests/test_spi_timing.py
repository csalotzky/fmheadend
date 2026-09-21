"""Execute the receiver's five instructions at 150 MHz against mode-0 edges.

This verifies digital timing, not PCB setup/hold or interrupt latency. Optional
pioasm header check ties the model to the compiled production instructions.
"""
import argparse
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--header", type=Path)
    args = parser.parse_args()
    source = (Path(__file__).parents[1] / "spi_audio.pio").read_text()
    instructions = []
    for line in source.splitlines():
        line = line.split(";")[0].strip()
        if line and not line.startswith(".") and not line.endswith(":"):
            instructions.append(re.sub(r"\s+", " ", line))
    assert instructions == ["wait 0 gpio 1", "wait 1 gpio 2", "in pins, 1",
                            "jmp x-- clock_low", "wait 0 gpio 2"]
    if args.header:
        text = args.header.read_text()
        opcodes = [int(x, 16) for x in re.findall(r"^\s*(0x[0-9a-f]+),", text, re.M)]
        assert opcodes == [0x2001, 0x2082, 0x4001, 0x0044, 0x2002]
        assert "spi_audio_wrap_target 1" in text and "spi_audio_wrap 4" in text
    data = bytes((i * 37 + (i >> 3)) & 255 for i in range(4100))
    # Six SM cycles per bit at 25 MHz. Sweep edge phase in tenths of a cycle,
    # with both input synchronizers retaining their normal two-cycle delay.
    for phase in range(10):
        for count in (32800, 32801, 32799):
            bits = [(data[i // 8] >> (7 - i % 8)) & 1 if i < 32800 else 1
                    for i in range(count)]
            pc = shift = shifted = 0
            x = 0xffffffff
            received = []
            for tick in range(count * 6 + 40):
                t = (tick - 2) * 10 - phase
                cs = int(t < 0 or t >= count * 60 + 10)
                pos = (t - 10) // 60
                sck = int(0 <= pos < count and (t - 10) % 60 < 30)
                # MOSI changes on the previous falling edge, half a bit ahead.
                idx = max(0, (t + 20) // 60)
                mosi = bits[min(idx, count - 1)]
                if pc == 0:
                    if cs == 0: pc = 1
                elif pc == 1:
                    if sck: pc = 2
                elif pc == 2:
                    shift = (shift << 1) | mosi
                    shifted += 1
                    if shifted == 8:
                        received.append(shift & 255)
                        shifted = 0
                    pc = 3
                elif pc == 3:
                    x = (x - 1) & 0xffffffff
                    pc = 4
                elif pc == 4:
                    if not sck: pc = 1
            assert (~x & 0xffffffff) == count
            assert bytes(received) == data[:count // 8]
            assert shifted == count % 8
    print("SPI PIO: 25 MHz mode0 phase sweep, MSB byte order and partial-bit counts PASS")


if __name__ == "__main__":
    main()
