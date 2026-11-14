"""Private PODCAST routing; feed/SQLite ownership stays in the parent."""
import sys
from unittest.mock import Mock

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.direct_pcm import DirectHttpFactory
from fmheadendd.sources.process_pipeline import ProcessPipeline, ExperimentalAudioFactory


def test_podcast_direct_is_separate_opt_in(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    db=Database(tmp_path/'podcast.db')
    s=db.save('sources',InputSourceConfig(name='podcast',type='PODCAST',config={'feeds':[{'url':'http://localhost/rss'}]}))
    try:
        for factory in (DirectHttpFactory(tmp_path),DirectHttpFactory(tmp_path,local=True)):
            with pytest.raises(DependencyUnavailable):factory(s)
        p=DirectHttpFactory(tmp_path,podcast=True)(s,podcast_database_path=str(tmp_path/'podcast.db'))
        assert p.direct_runtime==str(tmp_path) and p._podcast_service is p._catalog.service
        assert p._podcast_service.database_path==str(tmp_path/'podcast.db')
        assert 'podcast_database_path' not in p._create['options']
        assert p._podcast_service._io_thread is None
        assert ProcessPipeline(s,podcast_database_path=str(tmp_path/'podcast.db')).direct_runtime is None
        assert db.connection.execute('PRAGMA user_version').fetchone()[0]==1
    finally:db.close()


@pytest.mark.parametrize('enabled',[False,True])
def test_private_podcast_selection_and_defaults(monkeypatch,enabled):
    import uvicorn
    from fmheadendd import app
    monkeypatch.setattr(sys,'platform','linux')
    for variable in ('FMHEADEND_DIRECT_HTTP_PROTOTYPE','FMHEADEND_DIRECT_LOCAL_PROTOTYPE'):
        monkeypatch.delenv(variable,raising=False)
    monkeypatch.setenv('FMHEADEND_DIRECT_PODCAST_PROTOTYPE','1' if enabled else '0')
    monkeypatch.setattr(sys,'argv',['fmheadendd','--experimental-audio-worker','--mock-controller'])
    create=Mock();monkeypatch.setattr(app,'create_app',create);monkeypatch.setattr(uvicorn,'run',Mock())
    app.main();factory=create.call_args.kwargs['pipeline_factory']
    if enabled:assert isinstance(factory,DirectHttpFactory) and factory.podcast and not factory.local
    else:assert isinstance(factory,ExperimentalAudioFactory)
