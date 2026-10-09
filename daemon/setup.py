"""Build the Linux-only ctypes library; no Python/Lilv headers are required."""
import sys

from setuptools import Extension, setup

setup(ext_modules=[Extension('fmheadendd.sources._lv2_lifecycle',
    sources=['sources/lv2_lifecycle.c'], libraries=['dl', 'pthread'],
    extra_link_args=['-Wl,-Bsymbolic-functions'])] if sys.platform == 'linux' else [])
