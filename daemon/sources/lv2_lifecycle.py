"""Install the native Lilv lifecycle mutex before GStreamer loads gst-lv2.

Gst-lv2 instantiates on streaming threads, after set_state(PLAYING) returns.
A Python lock around pipeline construction cannot protect that operation.
"""
import ctypes
from pathlib import Path
import sys
import sysconfig
from threading import Lock

from ..errors import DependencyUnavailable

_lock = Lock()
_library = None  # Keep the global symbol provider loaded for process lifetime.


def ensure_lv2_lifecycle():
    global _library
    if sys.platform != 'linux':
        return
    with _lock:
        if _library is not None:
            return
        path = Path(__file__).with_name('_lv2_lifecycle' + sysconfig.get_config_var('EXT_SUFFIX'))
        try:
            library = ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
            library.fmheadend_lv2_guard_install.restype = ctypes.c_char_p
            error = library.fmheadend_lv2_guard_install()
            if error:
                raise OSError(error.decode('utf-8', errors='replace'))
        except (OSError, AttributeError) as exc:
            raise DependencyUnavailable(
                'Native LV2 lifecycle guard unavailable; rebuild/install daemon '
                f'with pip install ./daemon and start a fresh process: {exc}') from exc
        _library = library
