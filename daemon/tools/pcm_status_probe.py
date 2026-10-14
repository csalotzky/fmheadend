"""Read-only, bounded-memory PCM counter sampler. Writes JSON lines to stdout.

Uses cached /api/v1/status only; does not issue lifecycle or hardware commands.
Run a new window after each explicit source configuration change has settled.
"""
import argparse
import json
import time
from urllib.request import urlopen


SYSTEM_COUNTERS = ('mux_frames_sent', 'mux_late_frames', 'mux_transport_errors',
                   'spi_errors', 'controller_spi_audio_link_error_count')
TX_COUNTERS = ('underruns', 'overruns', 'pcm_bytes_received', 'pcm_bytes_consumed',
               'audio_underrun_count')


def counters(status):
    result = {f'system.{key}': status['system'].get(key) for key in SYSTEM_COUNTERS}
    for tx in status['modulators']:
        prefix = f'TX{tx["id"]}.'
        result.update({prefix + key: tx.get(key) for key in TX_COUNTERS})
        for stage in ('pcm_staging', 'pcm_consumer', 'pcm_writer'):
            for key, value in (tx.get(stage) or {}).items():
                if key != 'buffered_bytes':
                    result[prefix + stage + '.' + key] = value
    return result


def delta(before, after):
    # Null/reset counters are never reported as healthy zero deltas.
    return {key: value - before[key] if type(value) is int and
            type(before.get(key)) is int and value >= before[key] else None
            for key, value in after.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--label', default='pcm')
    args = parser.parse_args()
    if not 0 < args.seconds <= 3600 or not .25 <= args.interval <= 60:
        parser.error('seconds must be 0..3600 and interval 0.25..60')
    started = time.monotonic()
    first = previous = None
    while True:
        with urlopen(args.url.rstrip('/') + '/api/v1/status', timeout=10) as response:
            status = json.load(response)
        now = time.monotonic()
        current = counters(status)
        if first is None:
            first = current
        print(json.dumps(dict(label=args.label, elapsed_s=now-started, status=status,
            interval_delta=None if previous is None else delta(previous, current),
            total_delta=delta(first, current))), flush=True)
        previous = current
        if now-started >= args.seconds:
            break
        time.sleep(min(args.interval, args.seconds-(now-started)))


if __name__ == '__main__':
    main()
