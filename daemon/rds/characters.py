"""Deterministic subset of EN 50067 Annex E basic RDS characters."""
import unicodedata

# ASCII positions with different RDS glyphs are intentionally excluded.
SAFE = frozenset(range(0x20, 0x7f)) - {0x24, 0x5e, 0x60, 0x7e}
TRANSLITERATE = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                 "\u2013": '-', "\u2014": '-', "\u00df": 'ss', "\u00c6": 'AE',
                 "\u00e6": 'ae', "\u0152": 'OE', "\u0153": 'oe', "\u00d8": 'O', "\u00f8": 'o'}


def convert(text: str, limit: int) -> bytes:
    result = bytearray()
    for char in unicodedata.normalize('NFC', text):
        mapped = TRANSLITERATE.get(char, char)
        plain = ''.join(c for c in unicodedata.normalize('NFKD', mapped) if not unicodedata.combining(c))
        result.extend(ord(c) if ord(c) in SAFE else 0x3f for c in plain)
        if len(result) >= limit:
            break
    return bytes(result[:limit])


def ps_bytes(text: str) -> bytes:
    return convert(text, 8).ljust(8, b' ')


def rt_bytes(text: str) -> bytes:
    data = convert(text, 64)
    if len(data) < 64:
        data += b'\r'
    return data.ljust((len(data) + 3) // 4 * 4, b' ')
