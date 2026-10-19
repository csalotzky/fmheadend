"""Wait for fake-I/O job completions in tests, never in the media worker."""
import time


def settle(playback):
    deadline = time.monotonic() + 3
    while True:
        playback._service_jobs()
        jobs = [job for job in (playback.scan, playback.build[0] if playback.build else None)
                if job is not None]
        if not jobs:
            return
        for job in jobs:
            assert job.done.wait(max(0, deadline - time.monotonic())), 'LOCAL job did not complete'


def initial(playback):
    settle(playback)
    uri = playback.initial()
    settle(playback)
    return uri
