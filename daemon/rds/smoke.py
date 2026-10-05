"""Run with python -m fmheadendd.rds.smoke. No physical hardware access."""
from collections import Counter
import json
from pathlib import Path
import tempfile
from ..config import Settings
from ..database import Database
from ..models.configuration import ModulatorPatch, RtOverride
from ..modulators.manager import ModulatorManager
from ..mux.client import MockMuxClient
from ..rp2354.client import MockRp2354Client
from ..sources.manager import SourceManager
from .radiotext import RadioTextService
from .refill import RdsRefill


def run() -> dict:
    now = [0.0]
    clock = lambda: now[0]
    with tempfile.TemporaryDirectory(prefix='fm-rds-') as directory:
        db = Database(Path(directory) / 'smoke.db')
        try:
            sources = SourceManager(db)
            rp = MockRp2354Client(clock=clock)
            manager = ModulatorManager(db, sources, rp, MockMuxClient(), Settings(Path(directory) / 'smoke.db'))
            rt = RadioTextService(db, sources, clock)
            refill = RdsRefill(db, manager, rt, rp, clock)
            manager.rds = refill
            manager.update(0, ModulatorPatch(audio_mode="ANALOG", frequency_khz=98100,
                rf_attenuation_db=6.0, rds_enabled=True, rds_pi=0x1234, rds_ecc=0xe0,
                rds_pty=10, rds_tp=True, rds_ps=[{'text':'RADIO FM','duration_s':5}],
                rds_af_khz=[101100], rds_rt='Persistent fallback', rds_rt_enabled=True))
            manager.start(0)
            rt.set(0, RtOverride(text='Temporary programme', ttl_s=2))
            selected = rt.effective(0).model_dump()
            for i in range(61):
                now[0] = i * 0.1
                refill.tick()
            counts = Counter(f'{g[1] >> 12}A' for _, g in rp.rds_history)
            first = {}
            for _, group in rp.rds_history:
                first.setdefault(f'{group[1] >> 12}A', [f'{word:04X}' for word in group])
            result = {'physical_transmission_tested':False, 'configuration':db.get('modulators',0).model_dump(mode='json'),
                'initial_rt':selected, 'after_ttl':rt.effective(0).model_dump(),
                'groups_accepted':manager.statuses[0].rds_groups_accepted,
                'group_types':dict(counts), 'first_information_words':first,
                'fifo_queries':sum(call[0]=='GET_RDS_FIFO_FREE' for call in rp.rds_calls),
                'push_calls':sum(call[0]=='RDS_PUSH' for call in rp.rds_calls)}
            assert all(counts[k] for k in ('0A','1A','2A'))
            manager.stop(0)
            return result
        finally:
            db.close()


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
