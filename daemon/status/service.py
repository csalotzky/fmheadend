import time
import logging

from .host_metrics import HostMetrics
from .spi_diagnostics import SpiDiagnostics
from ..errors import DependencyUnavailable, DomainError
from .. import __version__
from ..regions import effective_plan
from ..rp2354.linux_client import Rp2354Client
from ..rp2354.protocol import IncompatibleProtocol, RpResponseError
from ..models.runtime_status import HeadendStatus, SystemStatus, TxState, WriterPcmStatus, InputSourceStatus


logger = logging.getLogger(__name__)


def communication_state(error):
    if isinstance(error, IncompatibleProtocol):
        return 'INCOMPATIBLE'
    # An explicit error reply proves communication, not a valid status snapshot.
    return 'AVAILABLE' if isinstance(error, RpResponseError) else 'UNAVAILABLE'


class StatusService:
    def __init__(self, modulators, sources, rp, mux, clock=time.monotonic, recovery=None,
                 config=None, radiotext=None):
        self.modulators, self.sources, self.rp, self.mux = modulators, sources, rp, mux
        self.radiotext = radiotext
        self.clock, self.started = clock, clock()
        self.host_metrics = HostMetrics(clock)
        self.spi_diagnostics = SpiDiagnostics(rp, clock)
        self.recovery = recovery
        self.config = config
        self._publication_failed = False
        self._mux_state = None
        self._observed_at = None
        self._modulator_configs = {}
        self.real_rp = isinstance(rp, Rp2354Client)
        self.system = SystemStatus(mocked=not self.real_rp)
        self.info = {"fmheadend_version": __version__, "controller_protocol_version": None,
                     "controller_firmware_version": None, "hardware_target": None,
                     "modulator_count": None, "controller_capability_stale": True, "mocked": not self.real_rp}
        self.refresh()

    def publish(self, *, max_age_s=2.0):
        """Control thread only. Build privately, then replace one owned snapshot.

        Readers never collect Source diagnostics or enter PCM/control locks.
        Publication failure leaves the previous observation aging normally.
        """
        capabilities = self.recovery.verified_info if self.recovery else None
        for key in ('frequency_min_khz', 'frequency_max_khz', 'frequency_step_khz'):
            self.info[key] = capabilities[key] if capabilities else None
        self.info['frequency_plan'] = effective_plan(self.modulators.settings.region, capabilities)
        source_statuses = {}
        views = {}
        self._modulator_configs = {c.id: c for c in self.modulators.list()}
        if self.config is not None:
            configs = self.config.list('sources')
            profile_errors = {}  # One generation only; no mutable profile models.
            source_statuses = {s.id: self.sources.status(s.id, source=s,
                profile_errors=profile_errors).model_copy(deep=True) for s in configs}
            views = dict(sources=configs, modulators=[self._modulator_configs[i] for i in self.modulators.physical_ids],
                         info=dict(self.info), source_statuses=source_statuses)
        snapshot = self.snapshot(source_statuses).model_copy(deep=True)
        if self.radiotext is not None:
            modulator_configs = {config.id: config for config in views.get('modulators', ())}
            for row in snapshot.modulators:
                row.rds_rt_effective = self.radiotext.effective(
                    row.id, config=modulator_configs.get(row.id),
                    source_statuses=source_statuses).text
        self._published = (self.clock(), max_age_s, snapshot, views)
        self._publication_failed = False

    def publication_failed(self):
        self._publication_failed = True

    def publication_age(self):
        return max(0, self.clock() - self._published[0])

    def published_view(self, name):
        return self._published[3][name]

    def published_source(self, source_id):
        published_at, max_age_s, _, views = self._published
        source = views['source_statuses'].get(source_id)
        if source is None:
            raise DomainError(404, 'NOT_FOUND', f'sources resource {source_id} does not exist')
        age = max(0, self.clock() - published_at)
        return source.model_copy(update={'observation_age_s': age,
                                        'observation_stale': self._publication_failed or age > max_age_s})

    def published_snapshot(self):
        """ASGI reader: cached host metrics, no DB or audio/control access."""
        published_at, max_age_s, snapshot, _ = self._published
        age = max(0, self.clock() - published_at)
        system = snapshot.system.model_copy()
        system.cpu_percent, system.temperature_c = self.host_metrics.read()
        system.snapshot_age_s = age
        system.snapshot_stale = self._publication_failed or age > max_age_s
        system.uptime_s += age
        if system.controller_observation_age_s is not None:
            system.controller_observation_age_s += age
        if system.snapshot_stale:
            system.controller_observation_stale = True
        # Published nested models belong to this generation and are never mutated.
        return snapshot.model_copy(update={'system': system})

    def refresh(self, *, publish=True):
        errors = []
        try:
            rp_status = self.recovery.observe() if self.recovery else self.rp.get_status()
            self.system.controller_state = rp_status["state"]
            self.system.i2c_errors = rp_status["i2c_errors"]
            if self.recovery:
                info = self.recovery.verified_info
            else:
                info = (self.rp.info or self.rp.get_info()) if self.real_rp else self.rp.get_info()
            self.info.update(
                controller_protocol_version=info["rp_protocol_version"],
                controller_firmware_version=info["rp_firmware_version"],
                hardware_target=info["hardware_target"],
                modulator_count=info["modulator_count"],
            )
            self._observed_at = self.clock()
            self.system.controller_observation_stale = False
            self.system.controller_communication_state = 'AVAILABLE'
            if self.real_rp:
                self.system.controller_uptime_ms = rp_status["uptime_ms"]
                self.system.controller_spi_audio_link_error_count = rp_status["rp_spi_audio_link_error_count"]
                self.system.controller_last_error_code = rp_status.get("last_error_code")
                if rp_status["state"] != "OK":
                    errors.append("Controller hardware: " + (rp_status.get("last_error_code") or rp_status["state"]))
            for channel in rp_status["modulators"]:
                if channel['id'] not in self.modulators.physical_ids:
                    continue
                row = self.modulators.statuses[channel["id"]]
                row.hardware_tx_state = channel["tx_state"]
                row.tx_state = TxState(channel["tx_state"])
                row.rf_attenuation_db = channel["rf_attenuation_db"]
                row.input_saturated = channel.get("input_saturated") if self.modulators.get(row.id).audio_mode == "ANALOG" else None
                row.audio_underrun_count = channel["audio_underrun_count"]
                row.rds_underrun_count = channel["rds_underrun_count"]
                row.rds_fifo_free = channel["rds_fifo_free"]
                row.hardware_last_error = channel["last_error_code"]
                if row.hardware_tx_state == "ERROR":
                    errors.append(f"Controller TX{row.id}: {row.hardware_last_error}")
                elif row.hardware_tx_state != ('ON' if (self.recovery.expected_on(row.id) if self.recovery
                        else self.modulators.desired_on(row.id)) else 'OFF'):
                    errors.append(f"Controller TX{row.id}: observed state differs from desired state")
                # Attenuation readback is the firmware's applied nominal value;
                # quantization can legitimately differ from the saved request.
        except DependencyUnavailable as exc:
            self.system.controller_communication_state = communication_state(exc)
            self.system.controller_observation_stale = True
            self.system.i2c_errors = self.rp.i2c_errors
            if self._observed_at is None:
                self.system.controller_state = 'UNKNOWN'
            errors.append(str(exc))
        if self.recovery:
            if self.recovery.verified_info is not None:
                self.info['modulator_count'] = self.recovery.verified_info['modulator_count']
            self.info['controller_capability_stale'] = not self.recovery.capability_current
        try:
            mux_status = self.mux.get_status()
            if self.recovery:
                mux_status = self.recovery.reconcile_mux(mux_status)
            self.system.mux_state = mux_status["state"]
            self.system.spi_errors = mux_status["spi_errors"]
            self.system.mux_transport = mux_status.get("transport")
            self.system.mux_transport_state = mux_status.get("transport_state", "RUNNING")
            self.system.mux_frames_sent = mux_status["frames_sent"]
            self.system.mux_late_frames = mux_status.get("late_frames", 0)
            self.system.mux_transport_errors = mux_status.get("transport_errors", 0)
            self.system.mux_mocked = mux_status.get("mocked")
            if mux_status.get('stream_state') != 'STREAMING' or (self.recovery and
                    self.recovery.verified_info is not None and
                    mux_status['tx_count'] != self.recovery.verified_info['modulator_count']):
                self.system.mux_state = 'ERROR'
                errors.append('Mux is not streaming with the verified Controller TX count')
            if mux_status.get("last_error"):
                errors.append(mux_status["last_error"])
            if self.system.mux_transport_state == "ERROR":
                self.system.mux_state = "ERROR"
            for channel in mux_status["channels"]:
                row = self.modulators.statuses[channel["id"]]
                row.underruns = channel["underruns"]
                row.overruns = channel["overruns"]
                row.pcm_bytes_received = channel.get("bytes_received", 0)
                row.pcm_bytes_consumed = channel.get("bytes_consumed", 0)
        except DependencyUnavailable as exc:
            self.system.mux_state = "ERROR"
            self.system.mux_transport_state = "ERROR"
            errors.append(str(exc))
        if self.system.mux_state != self._mux_state:
            logger.info('mux state: %s', self.system.mux_state)
            self._mux_state = self.system.mux_state
        self.system.last_error = "; ".join(errors) or None
        if publish:
            self.publish()

    def snapshot(self, source_statuses=None):
        # Control thread collection only: Source validation may access DB/files.
        system = self.system.model_copy(deep=True)
        system.global_stopped = self.modulators.global_stopped
        if self.recovery and self.recovery.error is not None:
            system.controller_communication_state = communication_state(self.recovery.error)
            system.controller_observation_stale = True
            system.last_error = str(self.recovery.error)
        system.controller_observation_age_s = None if self._observed_at is None else max(0, self.clock() - self._observed_at)
        if self.recovery and self.recovery.hardware_error and not self.recovery.error:
            system.last_error = self.recovery.hardware_error
        system.uptime_s = max(0, self.clock() - self.started)
        rows = []
        source_statuses = {} if source_statuses is None else dict(source_statuses)
        writers = self.mux.pcm_snapshot() if hasattr(self.mux, 'pcm_snapshot') else {}
        for status in self.modulators.statuses.values():
            if status.id not in self.modulators.physical_ids:
                continue
            row = status.model_copy(deep=True)
            config = self._modulator_configs[row.id]
            source_id = config.input_source_id if config.audio_mode != 'ANALOG' else None
            if source_id is not None:
                if source_id not in source_statuses:
                    source_statuses[source_id] = self.sources.statuses.get(source_id, InputSourceStatus(source_id=source_id))
                source = source_statuses[source_id]
                if not (self.recovery and row.id in self.recovery.audio_pending):
                    row.source_state = source.state.value
                    row.source_last_error = source.last_error
                    row.source_error_scope = source.error_scope
                if source.pcm is not None:
                    row.pcm_staging = source.pcm.staging
                    row.pcm_consumer = source.pcm.consumers.get(row.id)
            if row.id in writers:
                row.pcm_writer = WriterPcmStatus(**writers[row.id])
            if self.modulators.rds and self.modulators.rds.suspended and config.rds_enabled:
                row.rds_state = 'SUSPENDED'
            rows.append(row)
        if system.last_error or system.mux_state != "RUNNING" or any(r.tx_state == TxState.ERROR or
                (r.source_last_error and r.source_error_scope == 'INTERNAL') or
                r.rds_last_error or r.last_error for r in rows):
            system.system_state = "DEGRADED"
        return HeadendStatus(system=system, modulators=rows)
