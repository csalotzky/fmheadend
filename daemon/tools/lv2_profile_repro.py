"""Bounded profile-save reproducer: real Gst, isolated files, mock RF/mux.

Run under GDB with ``-ex run -ex 'thread apply all bt 20'``. The original
profile is only read. All API mutations target a private temporary store.
"""
import argparse
import copy
import json
import logging
from pathlib import Path
import tempfile
import time

from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.sources.gst_pipeline import GstPipeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, type=Path)
    parser.add_argument('--uri', required=True)
    parser.add_argument('--sources', type=int, choices=range(1, 9), required=True)
    parser.add_argument('--affected', type=int, required=True)
    parser.add_argument('--toggles', type=int, default=30)
    parser.add_argument('--telemetry', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.affected <= args.sources or not 1 <= args.toggles <= 100:
        parser.error('affected must be 1..sources; toggles must be 1..100')
    logging.basicConfig(level=logging.WARNING)
    original = json.loads(args.profile.read_text())
    enabled = original['stages']['wideband']['selected']
    if enabled == 'OFF':
        enabled = 'LSP_COMPRESSOR'
    assert enabled in original['stages']['wideband']['implementations']
    with tempfile.TemporaryDirectory(prefix='fmheadend-lv2-repro-') as directory:
        root = Path(directory)
        for kind in ('default', 'custom'):
            (root / kind).mkdir()
        for name in ('moderate_loud.json', 'unchanged.json'):
            (root / 'custom' / name).write_text(json.dumps(original))
        settings = Settings(root / 'db', audio_profiles_default_dir=root / 'default',
                            audio_profiles_custom_dir=root / 'custom')
        app = create_app(settings, pipeline_factory=GstPipeline)
        with TestClient(app) as api:
            def request(method, path, body=None):
                response = api.request(method, '/api/v1/' + path, json=body)
                response.raise_for_status()
                return response.json()

            for index in range(args.sources):
                profile = 'moderate_loud.json' if index < args.affected else 'unchanged.json'
                source = request('POST', 'sources', dict(name=f'Repro {index}', type='HTTP',
                    config=dict(uri=args.uri), use_default_audio_profile=False,
                    audio_profile_id='custom/' + profile))
                request('PATCH', f'modulators/{index}', dict(input_source_id=source['id'],
                    frequency_khz=90000 + index * 200, rf_attenuation_db=22.5))
                request('POST', f'modulators/{index}/start')
            if args.telemetry:
                # Exercise the real worker's meter collection without taking the
                # production telemetry socket or adding a second audio worker.
                api.portal.call(app.state.control.call,
                                lambda: app.state.services.sources.set_telemetry(True, 50))
            def wait_running():
                deadline = time.monotonic() + 20
                while True:
                    snapshots = {sid: p.snapshot() for sid, p in app.state.services.sources.pipelines.items()}
                    if all(s['state'] == 'RUNNING' for s in snapshots.values()):
                        return snapshots
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'Not all sources playing: {snapshots}')
                    time.sleep(.1)
            selected = original['stages']['wideband']['selected']
            for iteration in range(args.toggles):
                snapshots = wait_running()
                payload = copy.deepcopy(original)
                selected = enabled if selected == 'OFF' else 'OFF'
                payload['stages']['wideband']['selected'] = selected
                print(json.dumps(dict(event='toggle',iteration=iteration + 1,
                    sources=len(snapshots),affected=args.affected,states=snapshots,
                    mutation={'stages.wideband.selected': selected},monotonic=time.monotonic())), flush=True)
                result = request('PUT', 'audio-profiles/files/custom/moderate_loud.json', payload)
                if not result['applied']:
                    raise RuntimeError(result['restart_errors'])
                time.sleep(.15)
            wait_running()
        print(json.dumps(dict(event='passed',sources=args.sources,affected=args.affected,
                              toggles=args.toggles)), flush=True)


if __name__ == '__main__':
    main()
