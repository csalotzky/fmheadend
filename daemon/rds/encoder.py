"""EN 50067:1998 information blocks only. No checkwords or transport."""
Group = tuple[int, int, int, int]
NO_AF = 0xE0CD  # Table 11: no AF (224), filler (205).


def unsigned(value: int, bits: int) -> int:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f'Expected uint{bits}')
    return value


def block_b(kind: int, pty: int, tp: bool) -> int:
    if type(tp) is not bool:
        raise ValueError('TP must be boolean')
    return unsigned(kind, 4) << 12 | int(tp) << 10 | unsigned(pty, 5) << 5


def af_bytes(frequencies_khz) -> bytes:
    if len(frequencies_khz) > 25:
        raise ValueError('AF supports at most 25 frequencies')
    if not frequencies_khz:
        return bytes((224, 205))
    codes = []
    for frequency in frequencies_khz:
        if type(frequency) is not int or not 87600 <= frequency <= 107900 or frequency % 100:
            raise ValueError('AF must be 87600..107900 kHz on the 100 kHz raster')
        codes.append((frequency - 87500) // 100)
    data = bytes((224 + len(codes), *codes))
    return data if len(data) % 2 == 0 else data + bytes((205,))


def group_0a(pi: int, pty: int, tp: bool, ps: bytes, segment: int, stereo: bool = True, af=(), af_pair: int = 0) -> Group:
    unsigned(segment, 2)
    if len(ps) != 8 or type(stereo) is not bool:
        raise ValueError('PS needs eight encoded characters and boolean stereo')
    # DI d0 (stereo) is sent at address 3; d1/d2/d3=0. TA=0, M/S=1 (music).
    b = block_b(0, pty, tp) | 8 | (int(stereo and segment == 3) << 2) | segment
    encoded_af = af_bytes(af)
    offset = (af_pair % (len(encoded_af) // 2)) * 2
    return unsigned(pi, 16), b, int.from_bytes(encoded_af[offset:offset+2], "big"), int.from_bytes(ps[2*segment:2*segment+2], 'big')


def group_1a(pi: int, pty: int, tp: bool, ecc: int) -> Group:
    # Annex D.2 variant 0: LA=0, variant=000, paging OPC=0, ECC in C[7:0].
    # B low five bits=0 (no paging), D PIN=0 (not configured).
    return unsigned(pi, 16), block_b(1, pty, tp), unsigned(ecc, 8), 0


def group_2a(pi: int, pty: int, tp: bool, rt: bytes, segment: int, text_ab: int) -> Group:
    unsigned(segment, 4); unsigned(text_ab, 1)
    if len(rt) not in range(4, 65, 4) or segment * 4 >= len(rt):
        raise ValueError('RT segment outside encoded text')
    part = rt[segment*4:segment*4+4]
    return (unsigned(pi, 16), block_b(2, pty, tp) | text_ab << 4 | segment,
            int.from_bytes(part[:2], 'big'), int.from_bytes(part[2:], 'big'))
