"""Build the Linux-only native helpers."""
import shlex
import subprocess
import sys

from setuptools import Extension, setup

extensions = []
if sys.platform == 'linux':
    extensions.append(Extension('fmheadendd.sources._lv2_lifecycle',
        sources=['sources/lv2_lifecycle.c'], libraries=['dl', 'pthread'],
        extra_link_args=['-Wl,-Bsymbolic-functions']))
    # The production installer already supplies these headers. Keep CLI-only
    # builds usable without GStreamer and retain the stock RTP graph there.
    try:
        cflags = subprocess.check_output(['pkg-config', '--cflags', 'gstreamer-1.0'],
                                          text=True, stderr=subprocess.DEVNULL)
        libs = subprocess.check_output(['pkg-config', '--libs', 'gstreamer-1.0'],
                                        text=True, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    else:
        extensions.append(Extension('fmheadendd.sources._rtp_pcm_aggregate',
            sources=['sources/rtp_pcm_aggregate.c'], extra_compile_args=shlex.split(cflags),
            extra_link_args=shlex.split(libs)))

setup(ext_modules=extensions)
