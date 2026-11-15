"""Startup selection only: no native pipeline, controller, RF or service IO."""
from unittest.mock import Mock
import sys
import pytest
from fmheadendd import app

KINDS = ('HTTP','LOCAL','PODCAST','ALSA','NETWORK')


@pytest.fixture
def launch(monkeypatch):
    import uvicorn
    for kind in KINDS:monkeypatch.delenv(f'FMHEADEND_DIRECT_{kind}_PROTOTYPE',raising=False)
    create,run,rp=Mock(),Mock(),Mock()
    monkeypatch.setattr(app,'create_app',create)
    monkeypatch.setattr(uvicorn,'run',run)
    monkeypatch.setattr(app,'Rp2354Client',rp)
    def start(flags,platform='linux'):
        monkeypatch.setattr(sys,'platform',platform)
        monkeypatch.setattr(sys,'argv',['fmheadendd',*flags])
        app.main()
        return create.call_args.kwargs
    return start,create,run,rp


@pytest.mark.parametrize('flags,platform,factory',[
    ([], 'linux','DirectHttpFactory'),
    (['--audio-backend','audiod'],'linux','DirectHttpFactory'),
    (['--audio-backend','in-process'],'linux','GstPipeline'),
    (['--mock-controller'],'linux','DirectHttpFactory'),
    (['--mock-mux'],'linux','GstPipeline'),
    (['--mock'],'linux','MockPipeline'),
    (['--audio-backend','in-process','--mock'],'linux','MockPipeline'),
    (['--audio-backend','in-process','--mock-mux'],'linux','GstPipeline'),
    ([], 'win32','GstPipeline'),
    (['--mock'],'win32','MockPipeline'),
    (['--experimental-http-worker'],'linux','ExperimentalHttpFactory'),
    (['--experimental-audio-worker','--mock-mux'],'linux','ExperimentalAudioFactory'),
])
def test_selection_matrix(launch,flags,platform,factory):
    start,create,run,rp=launch
    selected=start(flags,platform)
    f=selected['pipeline_factory']
    assert getattr(f,'__name__',type(f).__name__)==factory
    if factory=='DirectHttpFactory':
        assert f.local and f.podcast and f.alsa and f.network
        assert selected['mux_factory'] is not None
    assert create.call_count==run.call_count==1


@pytest.mark.parametrize('flags,platform',[
    (['--audio-backend','audiod','--mock'],'linux'),
    (['--audio-backend','audiod','--mock-mux'],'linux'),
    (['--audio-backend','audiod'],'win32'),
    (['--experimental-audio-worker'],'win32'),
    (['--experimental-audio-worker','--mock'],'linux'),
    (['--audio-backend','in-process','--experimental-audio-worker'],'linux'),
    (['--audio-backend','audiod','--experimental-http-worker'],'linux'),
])
def test_invalid_combinations_fail_before_hardware(launch,flags,platform):
    start,create,run,rp=launch
    with pytest.raises(SystemExit) as exc:start(flags,platform)
    assert exc.value.code==2
    create.assert_not_called();rp.assert_not_called();run.assert_not_called()


@pytest.mark.parametrize('kind',KINDS)
def test_legacy_prototypes_keep_their_activation_contract(launch,monkeypatch,kind):
    start,create,run,rp=launch
    monkeypatch.setenv(f'FMHEADEND_DIRECT_{kind}_PROTOTYPE','1')
    switch='--experimental-http-worker' if kind=='HTTP' else '--experimental-audio-worker'
    selected=start([switch,'--mock-controller'])
    assert type(selected['pipeline_factory']).__name__=='DirectHttpFactory'
    with pytest.raises(SystemExit):start(['--audio-backend','in-process'])
    with pytest.raises(SystemExit):start(['--audio-backend','audiod'])
    with pytest.raises(SystemExit):start([])


def test_default_does_not_fallback_after_startup_failure(launch):
    start,create,run,rp=launch
    create.side_effect=RuntimeError('native startup unavailable')
    with pytest.raises(RuntimeError,match='native startup unavailable'):start([])
    assert type(create.call_args.kwargs['pipeline_factory']).__name__=='DirectHttpFactory'
    assert create.call_count==1
    run.assert_not_called()
