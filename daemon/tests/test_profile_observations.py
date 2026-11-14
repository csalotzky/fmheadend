"""Status observes applied state; configuration boundaries load profiles."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.sources.manager import SourceManager
from fmheadendd.errors import DomainError

DEFAULT = Path(__file__).resolve().parents[2] / 'audio-profiles/default'

@pytest.mark.parametrize('paths', [['default/basic_filtering.json'] * 8,
                                 ['default/basic_filtering.json', 'custom/missing.json']])
def test_runtime_observation_never_loads_profiles(tmp_path, paths):
    files = AudioProfileFiles(DEFAULT, tmp_path)
    manager = SourceManager(None, profile_files=files)
    sources = [SimpleNamespace(id=i, use_default_audio_profile=False, audio_profile_id=path)
               for i, path in enumerate(paths)]
    for s in sources:
        manager.pipelines[s.id] = SimpleNamespace(mocked=False,
            snapshot=lambda:dict(state='RUNNING',stream_title='Current'),
            pcm_snapshot=lambda:dict(staging={'accepted_bytes':100},consumers={}))
    with patch.object(files, 'get', side_effect=AssertionError('Runtime profile read')):
        for _ in range(20):
            observed = [manager.status(s.id,source=s) for s in sources]
            assert all(s.state == 'RUNNING' for s in observed)
            observed[0].stream_title = 'Changed'
            observed[0].pcm.staging.accepted_bytes = 999
        fresh = manager.status(sources[0].id,source=sources[0])
        assert fresh.stream_title == 'Current' and fresh.pcm.staging.accepted_bytes == 100

def test_explicit_load_and_failed_apply_status(tmp_path):
    manager = SourceManager(None,profile_files=AudioProfileFiles(DEFAULT,tmp_path))
    s = SimpleNamespace(id=1,use_default_audio_profile=False,audio_profile_id='custom/missing.json')
    with pytest.raises(DomainError,match='Profile not found'):
        manager.validate_config(s)
    from fmheadendd.models.runtime_status import InputSourceStatus
    manager.restart_errors[s.id] = 'Profile load failed'
    manager.statuses[s.id] = InputSourceStatus(source_id=s.id,state='ERROR',last_error='Profile load failed')
    manager.pipelines[s.id] = SimpleNamespace(mocked=False,snapshot=lambda:dict(state='RUNNING'))
    assert manager.status(s.id,source=s).last_error == 'Profile load failed'
