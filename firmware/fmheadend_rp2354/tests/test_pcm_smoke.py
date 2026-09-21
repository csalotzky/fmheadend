import importlib.util
from pathlib import Path
import struct

spec = importlib.util.spec_from_file_location("pcm_smoke", Path(__file__).parents[1] / "tools/pcm_smoke.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for tx in range(8):
    assert module.pcm_block(tx, 0, 128, "constant") == struct.pack("<hh", 0x1234 + tx, -0x2345 - tx) * 128
    complete = module.pcm_block(tx, 0, 256, "tone")
    assert complete == module.pcm_block(tx, 0, 128, "tone") + module.pcm_block(tx, 128, 128, "tone")
    samples = struct.unpack("<512h", complete)
    assert samples[::2] == samples[1::2]
    assert max(abs(v) for v in samples) == 8231
print("Bench PCM: all lane identifiers, stereo tones and chunk continuity PASS")
