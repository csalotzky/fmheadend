"""Independent IEC 62106-4:2018 Table 5 vectors, not encoder round trips.

Numeric UCS-2 entries transcribed from Table 5 (pp. 14-20) as published at
https://www.theiteh.com/catalog/standards/iec/a9170081-7d7e-4d69-b316-231429d024c0/iec-62106-4-2018
Cross-checked with the Table 4 glyph matrix in the supplied IEC PDF. These
expected numeric identities are separate from the production glyph columns.
"""
import unicodedata

from fmheadendd.rds.characters import BASIC_RDS, convert, ps_bytes, rt_bytes
from fmheadendd.rds.encoder import group_0a, group_2a
from fmheadendd.rds.scheduler import Scheduler
from fmheadendd.models.configuration import ModulatorConfig, PsEntry

# Each line lists UCS-2 for consecutive RDS positions, high nibble 2..F.
# ---- is NOT ASSIGNED. Table 5 numeric identities take priority over glyph
# extraction / mislabeled prose (e.g. D3 is U+00CB, EB is U+0106).
TABLE5 = """
0020 0021 0022 0023 00A4 0025 0026 0027 0028 0029 002A 002B 002C 002D 002E 002F
0030 0031 0032 0033 0034 0035 0036 0037 0038 0039 003A 003B 003C 003D 003E 003F
0040 0041 0042 0043 0044 0045 0046 0047 0048 0049 004A 004B 004C 004D 004E 004F
0050 0051 0052 0053 0054 0055 0056 0057 0058 0059 005A 005B 005C 005D 2015 005F
2016 0061 0062 0063 0064 0065 0066 0067 0068 0069 006A 006B 006C 006D 006E 006F
0070 0071 0072 0073 0074 0075 0076 0077 0078 0079 007A 007B 007C 007D 203E ----
00E1 00E0 00E9 00E8 00ED 00EC 00F3 00F2 00FA 00F9 00D1 00C7 015E 00DF 00A1 0132
00E2 00E4 00EA 00EB 00EE 00EF 00F4 00F6 00FB 00FC 00F1 00E7 015F 011F 0131 0133
00AA 03B1 00A9 2030 011E 011B 0148 0151 03C0 20AC 00A3 0024 2190 2191 2192 2193
00BA 00B9 00B2 00B3 00B1 0130 0144 0171 00B5 00BF 00F7 00B0 00BC 00BD 00BE 00A7
00C1 00C0 00C9 00C8 00CD 00CC 00D3 00D2 00DA 00D9 0158 010C 0160 017D 00D0 013F
00C2 00C4 00CA 00CB 00CE 00CF 00D4 00D6 00DB 00DC 0159 010D 0161 017E 0111 0140
00C3 00C5 00C6 0152 0177 00DD 00D5 00D8 00DE 014A 0154 0106 015A 0179 0166 00F0
00E3 00E5 00E6 0153 0175 00FD 00F5 00F8 00FE 014B 0155 0107 015B 017A 0167 ----
"""


def test_complete_printable_mapping_against_table5_numeric_identities():
    points = TABLE5.split()
    assert len(points) == 224
    expected = {chr(int(point, 16)): byte for byte, point in enumerate(points, 0x20)
                if point != '----'}
    assert len(expected) == 222
    assert BASIC_RDS == expected
    for char, byte in expected.items():
        assert convert(char, 8) == bytes([byte]), f'U+{ord(char):04X}'


def test_hungarian_reference_bytes_and_canonical_equivalence():
    lower = '\u00e1\u00e9\u00ed\u00f3\u00f6\u0151\u00fa\u00fc\u0171'
    upper = '\u00c1\u00c9\u00cd\u00d3\u00d6\u0150\u00da\u00dc\u0170'
    assert convert(lower, 64) == bytes.fromhex('80 82 84 86 97 a7 88 99 b7')
    assert convert(upper, 64) == bytes.fromhex('c0 c2 c4 c6 d7 4f c8 d9 55')
    assert convert(unicodedata.normalize('NFD', lower), 64) == convert(lower, 64)
    assert convert(unicodedata.normalize('NFD', upper), 64) == convert(upper, 64)


def test_special_positions_and_supported_symbols_precede_transliteration():
    assert convert('$\u00a4\u2015\u2016\u203e|', 64) == bytes.fromhex('ab 24 5e 60 7e 7c')
    assert convert('^`~', 64) == b'???'
    assert convert('\u00df\u00c6\u00e6\u0152\u0153\u00d8\u00f8\u00bc\u00b5', 64) == bytes.fromhex('8d e2 f2 e3 f3 e7 f7 bc b8')
    assert convert('\u00d0\u0111\u0177\u0175\u011b', 64) == bytes.fromhex('ce de e4 f4 a5')
    assert convert('\u201cHi\u201d\u2014\u0141\u0142\u1e9e\u2026', 64) == b'"Hi"-LlSS...'
    assert convert('\U0001f600\u4e2d\u0301', 64) == b'???'
    controls = ''.join(chr(c) for c in [*range(32), *range(0x7f, 0xa0)])
    assert convert(controls, 128) == b'?' * len(controls)
    assert convert('\x1b\r\n\x0f', 64) == b'????'


def test_padding_and_expansion_before_byte_truncation():
    assert ps_bytes('\u00e9') == b'\x82' + b' ' * 7
    assert ps_bytes('1234567\u2026') == b'1234567.'
    assert ps_bytes('123456\ufb03') == b'123456ff'
    assert rt_bytes('x' * 62 + '\u2026') == b'x' * 62 + b'.\r'
    assert rt_bytes('x' * 62 + '\u2026', 'PAD') == b'x' * 62 + b'..'
    assert rt_bytes('\u0151\u0171') == b'\xa7\xb7\r '
    assert rt_bytes('\u0151\u0171', 'PAD') == b'\xa7\xb7' + b' ' * 62
    assert rt_bytes('\r') == b'?\r  '  # User CR is never a control code.
    assert convert('\u00e9', 0) == b''


def test_accented_ps_rt_exact_information_words():
    ps = ps_bytes('\u00e1\u00e9\u00ed\u00f3\u00f6\u0151\u00fc\u0171')
    words = [0x8082, 0x8486, 0x97a7, 0x99b7]
    for segment, word in enumerate(words):
        assert group_0a(0x1234, 0, False, ps, segment) == (
            0x1234, (8 | segment | (4 if segment == 3 else 0)), 0xe0cd, word)
    rt = rt_bytes('\u0151\u0171\u00c1\u00c9')
    assert group_2a(0x1234, 0, False, rt, 0, 1) == (0x1234, 0x2010, 0xa7b7, 0xc0c2)
    assert group_2a(0x1234, 0, False, rt, 1, 1) == (0x1234, 0x2011, 0x0d20, 0x2020)


def test_ab_uses_final_encoded_bytes_and_dynamic_ps_uses_same_boundary():
    c = ModulatorConfig(rds_ps=[PsEntry(text='\u00e9', duration_s=1), PsEntry(text='\u0151', duration_s=1)])
    s = Scheduler(); s.reset(c, '\u00e9', 0)
    assert not s.update_rt('e\u0301', .1) and s.text_ab == 0
    assert s.update_rt('\u00e8', .2) and s.text_ab == 1
    assert s.update_rt('e', .3) and s.text_ab == 0
    assert s.update_rt('x' * 63 + 'a', .4)
    assert not s.update_rt('x' * 63 + '\u00e9', .5)  # Outside CR byte budget.
    s.reset(c, '\u0150', 0)
    assert not s.update_rt('O', .1)  # Same fallback on the wire.
    first, s = s.prepare(6, 0)
    second, s = s.prepare(6, 1)
    assert next(g for g in first if g[1] >> 12 == 0)[3] == 0x8220
    assert next(g for g in second if g[1] >> 12 == 0)[3] == 0xa720
