"""LOCAL produces the ordinary source metadata; no extra RT selector or status."""
from fmheadendd.tests.profile_support import profile_store
from types import SimpleNamespace
from unittest.mock import Mock

from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig, RtOverride
from fmheadendd.rds.radiotext import RadioTextService
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.local_media import MediaItem
from fmheadendd.sources.local_playback import LocalPlayback
from fmheadendd.sources.manager import SourceManager


def pipeline(tmp_path):
    p = GstPipeline(InputSourceConfig(name='LOCAL', type='LOCAL', config=dict(mode='folder',path=str(tmp_path))))
    p.Gst = SimpleNamespace(EventType=SimpleNamespace(STREAM_START='start',TAG='tag'), PadProbeReturn=SimpleNamespace(OK='OK'))
    p._local = LocalPlayback(SimpleNamespace(order='sequential'), p._audio_ready,
                             resolver=lambda _: [MediaItem(tmp_path/'My Song.flac'), MediaItem(tmp_path/'Song B.flac')])
    p._local.initial()
    return p


def event(p, kind, **values):
    tags = Mock()
    tags.get_string.side_effect = lambda k: (k in values, values.get(k))
    tags.get_uint.side_effect = lambda k: (False,0)
    info = Mock()
    info.get_event.return_value = SimpleNamespace(type=kind,parse_tag=lambda:tags)
    p._local_event(None,info)


def metadata(p):
    s=p.snapshot()
    return (s['title'],s['artist'],s['stream_title'])


def test_render_boundary_handoff_reset_and_late_tags(tmp_path):
    p=pipeline(tmp_path)
    event(p,'start')
    assert metadata(p)==(None,None,None)  # preroll is not rendered PCM
    p._publish_local_metadata()
    assert metadata(p)==('My Song',None,'My Song')
    event(p,'tag',title='Actual Title',artist='Actual Artist')
    p._publish_local_metadata()
    assert metadata(p)==('Actual Title','Actual Artist','Actual Artist - Actual Title')
    p._local_handoff(Mock())
    assert metadata(p)==('Actual Title','Actual Artist','Actual Artist - Actual Title')
    # Old serialized tags still belong to A, even though B has been prepared.
    event(p,'tag',title='Last A')
    event(p,'start')
    p._publish_local_metadata()
    assert metadata(p)==('Song B',None,'Song B')
    event(p,'tag',title='Real B');p._publish_local_metadata()
    assert metadata(p)==('Real B',None,'Real B')
    event(p,'tag',artist='Artist B');p._publish_local_metadata()
    assert metadata(p)==('Real B','Artist B','Artist B - Real B')
    event(p,'tag',title='   ');p._publish_local_metadata()
    assert metadata(p)[0]=='Real B'


def test_remote_fallback_is_safe_and_uri_unchanged(tmp_path):
    p=pipeline(tmp_path)
    uri='https://user:password@example.org/audio/My%20Programme.mp3?token=secret#secret'
    p._local=LocalPlayback(SimpleNamespace(order='sequential'),p._audio_ready,resolver=lambda _:[MediaItem(uri)])
    assert p._local.initial()==uri
    event(p,'start');p._publish_local_metadata()
    assert metadata(p)==('My Programme',None,'My Programme')
    event(p,'tag',title='Remote Title');p._publish_local_metadata()
    assert metadata(p)[2]=='Remote Title'


def test_error_and_eos_recovery_clear_previous_metadata(tmp_path):
    for failed in (False,True):
        p=pipeline(tmp_path)
        event(p,'start');p._publish_local_metadata()
        p._pipeline=Mock();p._bus=Mock()
        p.Gst.State=SimpleNamespace(NULL='NULL',PLAYING='PLAYING')
        p._local_resume(failed=failed)
        assert metadata(p)==(None,None,None)
        event(p,'start');p._publish_local_metadata()
        assert metadata(p)==('Song B',None,'Song B')


def test_source_status_and_existing_radiotext_selection(tmp_path):
    db=Database(tmp_path/'metadata.db')
    p=pipeline(tmp_path)
    source=db.save('sources',p.source)
    sources=SourceManager(db, profile_files=profile_store(tmp_path))
    sources.pipelines[source.id]=p
    for tx in (0,1):
        saved=db.get('modulators',tx)
        db.save('modulators',saved.model_copy(update=dict(input_source_id=source.id,rds_metadata_to_rt=tx==0,rds_rt='Fallback')),tx)
    rt=RadioTextService(db,sources)
    try:
        event(p,'start');event(p,'tag',title='Track',artist='Artist');p._publish_local_metadata()
        assert sources.status(source.id).title=='Track'
        assert rt.effective(0).text=='Artist - Track' and rt.effective(0).origin=='metadata'
        assert rt.effective(1).text=='Fallback'
        rt.set(0,RtOverride(text='Override'))
        assert rt.effective(0).text=='Override'
        rt.clear(0)
        assert rt.effective(0).text=='Artist - Track'
        p._clear_local_metadata()
        assert rt.effective(0).text=='Fallback'
        assert db.get('sources',source.id).config==source.config
    finally:
        p.stop();db.close()
