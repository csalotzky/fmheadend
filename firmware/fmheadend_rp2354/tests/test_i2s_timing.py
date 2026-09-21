"""Execute the actual PIO opcodes and check wire edges, slots and lane bits.

No Pico/Python packages required. With --header, also compare against pioasm's
production build output so source parsing cannot conceal assembler differences.
"""
import argparse
from pathlib import Path
import re


def assemble(path):
    labels, rows = {}, []
    for line in path.read_text().splitlines():
        line = line.split(";", 1)[0].strip()
        if not line or line.startswith("."):
            continue
        if line.endswith(":"):
            labels[line[:-1]] = len(rows)
        else:
            rows.append(line)
    code = []
    for row in rows:
        op, side, delay = re.fullmatch(r"(.+?)\s+side\s+(0b[01]+)\s+\[(\d+)\]", row).groups()
        timing = (int(side, 2) << 11) | (int(delay) << 8)
        if op.strip() == "out pins, 8":
            instruction = 0x6008
        elif op.strip() == "set x, 14":
            instruction = 0xE02E
        else:
            label = re.fullmatch(r"jmp x--\s+(\w+)", op.strip())[1]
            instruction = 0x0040 | labels[label]
        code.append(instruction | timing)
    return code


def verify(code):
    # Distinct left/right and lane words reveal channel/polarity/alignment errors.
    frames = 7
    values = [[[(0x8123 + 7919*f + 2749*c + 997*lane) & 65535 for lane in range(8)]
               for c in range(2)] for f in range(frames)]
    data = [sum(((values[f][c][lane] >> bit) & 1) << lane for lane in range(8))
            for f in range(frames) for c in range(2) for bit in range(15, -1, -1)]
    pc, x, cursor, now, pins = 0, 14, 0, 0, 0
    edges = []
    # Every instruction holds side-set for its full duration, including delay.
    while len(edges) < len(data):
        instruction = code[pc]
        side = (instruction >> 11) & 3
        duration = 1 + ((instruction >> 8) & 7)
        op = instruction >> 13
        next_pc = (pc + 1) % len(code)
        if op == 3:
            assert instruction & 255 == 8
            pins = data[cursor]
            cursor += 1
        elif op == 0:
            assert (instruction >> 5) & 7 == 2  # JMP X--, post-decrement
            if x:
                next_pc = instruction & 31
            x = (x - 1) & 0xFFFFFFFF
        elif op == 7:
            assert (instruction >> 5) & 7 == 1  # SET X
            x = instruction & 31
        else:
            raise AssertionError(f"Unexpected opcode {instruction:04x}")
        if side & 1:
            edges.append((now, side >> 1, pins))
        else:
            assert duration == 2  # Uniform low half-cycle, including boundaries.
        assert duration == 2
        now += duration
        pc = next_pc
    assert cursor == frames*32
    assert now == frames*128
    for bit, (t, ws, value) in enumerate(edges):
        assert t == 2 + bit*4  # 1.536 MHz from 6.144 MHz, not SM frequency itself.
        # WS has switched already on the previous channel's bit zero.
        assert ws == ((bit + 1) // 16) % 2
        assert value == data[bit]
    # Decode a receiver: new WS selects the word starting on NEXT rising edge.
    for edge in range(15, len(edges)-16, 16):
        channel = edges[edge][1]
        word_index = (edge+1)//16
        for lane in range(8):
            sample = 0
            for row in edges[edge+1:edge+17]:
                sample = (sample << 1) | ((row[2] >> lane) & 1)
            assert sample == values[word_index//2][channel][lane]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", type=Path)
    args = parser.parse_args()
    code = assemble(Path(__file__).resolve().parents[1] / "i2s_audio.pio")
    if args.header:
        text = args.header.read_text().split("i2s_audio_program_instructions[] = {", 1)[1].split("};", 1)[0]
        actual = [int(s, 16) for s in re.findall(r"^\s*(0x[0-9a-fA-F]+),", text, re.M)]
        assert actual == code, (actual, code)
    verify(code)
    print("PIO opcodes: 128 cycles/frame, 32 BCLKs, WS polarity/delay, MSB-first stereo, eight lanes PASS")
