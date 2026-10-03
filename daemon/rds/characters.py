"""Unicode to printable basic RDS, IEC 62106-4:2018 section 5.2, Tables 4/5.

Table 4 columns are the high nibble, rows the low nibble. Table 5's UCS-2
identities disambiguate glyphs (notably 0x60, 0x7E and 0xCE). Source and
fallback policy: docs/milestone5.md#character-policy. No repertoire switching.
"""
import unicodedata

# Explicit Table 4 columns 2..F, each in row order 0..F. NUL placeholders
# represent UNASSIGNED slots, never output characters. This is not Latin-1
# or the revised DAB table. Unicode identities follow Table 5, including
# U+2016 DOUBLE VERTICAL LINE, U+203E OVERLINE, U+00D0 CAPITAL ETH.
_COLUMNS = (
    " !\"#¤%&'()*+,-./",  # 2
    "0123456789:;<=>?",   # 3
    "@ABCDEFGHIJKLMNO",   # 4
    "PQRSTUVWXYZ[\\]―_", # 5
    "‖abcdefghijklmno",   # 6
    "pqrstuvwxyz{|}‾\0", # 7
    "áàéèíìóòúùÑÇŞß¡Ĳ", # 8
    "âäêëîïôöûüñçşğıĳ", # 9
    "ªα©‰Ğěňőπ€£$←↑→↓", # A
    "º¹²³±İńűµ¿÷°¼½¾§", # B
    "ÁÀÉÈÍÌÓÒÚÙŘČŠŽÐĿ", # C
    "ÂÄÊËÎÏÔÖÛÜřčšžđŀ", # D
    "ÃÅÆŒŷÝÕØÞŊŔĆŚŹŦð", # E
    "ãåæœŵýõøþŋŕćśźŧ\0", # F
)
BASIC_RDS = {char: column * 16 + row
             for column, chars in enumerate(_COLUMNS, 2)
             for row, char in enumerate(chars) if char != '\0'}

# Only used for characters with no direct entry. Supported accents, ligatures,
# fractions and symbols must reach BASIC_RDS before compatibility decomposition.
TRANSLITERATE = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                 "\u2013": '-', "\u2014": '-', "\u2026": '...',
                 "\u0141": 'L', "\u0142": 'l', "\u1e9e": 'SS'}


def convert(text: str, limit: int) -> bytes:
    result = bytearray()
    for char in unicodedata.normalize('NFC', text):
        if char in BASIC_RDS:
            result.append(BASIC_RDS[char])
        else:
            mapped = TRANSLITERATE.get(char, char)
            plain = ''.join(c for c in unicodedata.normalize('NFKD', mapped)
                            if not unicodedata.combining(c))
            # Even a standalone unsupported combining mark has a visible fallback.
            result.extend(BASIC_RDS.get(c, 0x3f) for c in plain or '?')
        if len(result) >= limit:
            break
    return bytes(result[:limit])


def ps_bytes(text: str) -> bytes:
    return convert(text, 8).ljust(8, b' ')


def rt_bytes(text: str, termination: str = 'CR') -> bytes:
    if termination == 'PAD':
        return convert(text, 64).ljust(64, b' ')
    if termination != 'CR':
        raise ValueError('RT termination must be CR or PAD')
    # Reserve one byte so even maximum-length CR text is terminated.
    data = convert(text, 63) + b'\r'
    return data.ljust((len(data) + 3) // 4 * 4, b' ')
