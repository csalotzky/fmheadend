"""Register the optional native RTP L16 element after Gst.init()."""
import ctypes
import logging
from pathlib import Path
import sys
import sysconfig
from threading import Lock


_lock = Lock()
_library = None
_attempted = False


def register_rtp_pcm_aggregate():
    global _library, _attempted
    if sys.platform != 'linux':
        return False
    with _lock:
        if not _attempted:
            _attempted = True
            path = Path(__file__).with_name('_rtp_pcm_aggregate' + sysconfig.get_config_var('EXT_SUFFIX'))
            try:
                library = ctypes.CDLL(str(path))
                register = library.fmheadend_register_rtp_pcm_aggregate
                register.argtypes = []
                register.restype = ctypes.c_int
                if not register():
                    raise OSError('GStreamer registration failed')
                _library = library  # Keep the registered GType's code loaded.
            except (OSError, AttributeError) as exc:
                logging.getLogger(__name__).warning('RTP PCM aggregation unavailable, using original pipeline: %s', exc)
        return _library is not None
