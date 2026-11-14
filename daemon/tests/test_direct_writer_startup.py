"""A pre-grant heartbeat may arrive after the parent's first grant intent."""
import sys
import pytest
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.worker_reporting import accept_report
from fmheadendd.sources.audio_ipc import ProtocolError
from fmheadendd.tests.test_audio_worker import source


def test_delayed_grant_and_post_grant_writer_deadline(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    clock=[100.0]
    monkeypatch.setattr('time.monotonic',lambda:clock[0])
    p=ProcessPipeline(source(),direct_runtime=str(tmp_path))
    data=dict(blocks=0,bytes=0,produced_ns=0,writer_at=90.)
    accept_report(p,'heartbeat',data)  # No grant: an idle writer need not exist.
    p.set_output_lease(1,1)
    accept_report(p,'heartbeat',data)  # In-flight pre-grant heartbeat.
    clock[0]=104.9
    accept_report(p,'heartbeat',data)
    clock[0]=106.
    p.set_output_lease(1,2,channel=1)  # Another TX cannot reset the deadline.
    with pytest.raises(ProtocolError,match='writer stalled'):
        accept_report(p,'heartbeat',data)
    accept_report(p,'heartbeat',dict(data,writer_at=105.))
    p.set_output_lease(2,None)
    with pytest.raises(ProtocolError,match='writer stalled'):
        accept_report(p,'heartbeat',data)  # TX1 still granted.
    p.set_output_lease(2,None,channel=1)
    accept_report(p,'heartbeat',data)
    clock[0]=200.
    p.set_output_lease(3,3)
    accept_report(p,'heartbeat',data)  # A new active-writer epoch.
    p.set_output_lease(2,None)  # Stale revoke cannot reset ownership/deadline.
    assert p._direct_intents[0]==(3,3)
    clock[0]=206.
    with pytest.raises(ProtocolError,match='writer stalled'):
        accept_report(p,'heartbeat',data)
