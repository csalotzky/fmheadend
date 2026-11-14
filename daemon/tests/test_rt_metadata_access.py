from threading import Lock
from unittest.mock import patch
import pytest
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.manager import SourceManager
from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig, ModulatorConfig
from fmheadendd.models.runtime_status import InputSourceStatus
from fmheadendd.rds.radiotext import RadioTextService


def test_title_switch_restart_and_clear_without_status_rebuild():
    db = Database(':memory:')
    try:
        sources = SourceManager(db)
        ids = []
        for title in ('First', 'Second'):
            source = db.save('sources', InputSourceConfig(name=title, type='HTTP', config={'uri': 'https://example.org/live'}))
            ids.append(source.id)
            pipeline = ProcessPipeline.__new__(ProcessPipeline)
            pipeline._lock = Lock()
            pipeline._status = {'stream_title': title}
            sources.pipelines[source.id] = pipeline
        rt = RadioTextService(db, sources)
        config = ModulatorConfig(input_source_id=ids[0], rds_metadata_to_rt=True, rds_rt='Fallback')
        with patch.object(sources, 'status', side_effect=AssertionError('full status')):
            assert rt.effective(0, config=config).text == 'First'
            config.input_source_id = ids[1]
            assert rt.effective(0, config=config).text == 'Second'
            sources.pipelines[ids[1]]._status['stream_title'] = None
            assert rt.effective(0, config=config).text == 'Fallback'
            replacement = ProcessPipeline.__new__(ProcessPipeline)
            replacement._lock = Lock()
            replacement._status = {'stream_title': 'Restarted'}
            sources.pipelines[ids[1]] = replacement
            assert rt.effective(0, config=config).text == 'Restarted'
        sources.restart_errors[ids[1]] = 'Restart failed'
        sources.statuses[ids[1]] = InputSourceStatus(source_id=ids[1], state='ERROR')
        assert rt.effective(0, config=config).text == 'Fallback'
    finally:
        db.close()

@pytest.mark.parametrize('pipeline_class,field', [(GstPipeline, '_snapshot'), (ProcessPipeline, '_status')])
def test_metadata_only_read_is_fresh_and_does_not_snapshot(pipeline_class, field):
    pipeline = pipeline_class.__new__(pipeline_class)
    pipeline._lock = Lock()
    setattr(pipeline, field, {'stream_title': 'Artist - First'})
    with patch.object(pipeline_class, 'snapshot', side_effect=AssertionError('full snapshot')):
        assert pipeline.current_stream_title() == 'Artist - First'
        getattr(pipeline, field)['stream_title'] = 'Artist - Second'
        assert pipeline.current_stream_title() == 'Artist - Second'
        getattr(pipeline, field)['stream_title'] = None
        assert pipeline.current_stream_title() is None
