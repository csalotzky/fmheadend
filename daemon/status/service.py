import time
from ..errors import DependencyUnavailable
from .. import __version__
from ..rp2354.linux_client import Rp2354Client
from ..rp2354.protocol import IncompatibleProtocol
from ..models.runtime_status import HeadendStatus, SystemStatus, TxState


class StatusService:
    def __init__(self, modulators, sources, rp, mux, clock=time.monotonic):
        self.modulators, self.sources, self.rp, self.mux = modulators, sources, rp, mux
        self.clock, self.started = clock, clock()
        self.real_rp = isinstance(rp, Rp2354Client)
        self.system = SystemStatus(mocked=not self.real_rp)
        self.info = {"fmheadend_version": __version__, "rp_protocol_version": None,
                     "rp_firmware_version": None, "hardware_revision": None,
                     "modulator_count": 8, "mocked": not self.real_rp}
        self.refresh()

    def refresh(self):
        errors = []
        try:
            rp_status = self.rp.get_status()
            self.system.rp2354_state = rp_status["state"]
            self.system.i2c_errors = rp_status["i2c_errors"]
            self.info.update((self.rp.info or self.rp.get_info()) if self.real_rp else self.rp.get_info())
            if self.real_rp:
                self.system.rp_uptime_ms = rp_status["uptime_ms"]
                self.system.rp_spi_audio_link_error_count = rp_status["rp_spi_audio_link_error_count"]
                self.system.rp_last_error_code = rp_status.get("last_error_code")
                if rp_status["state"] != "OK":
                    errors.append("RP hardware: " + (rp_status.get("last_error_code") or rp_status["state"]))
                for channel in rp_status["modulators"]:
                    row = self.modulators.statuses[channel["id"]]
                    row.hardware_tx_state = channel["tx_state"]
                    row.tx_state = TxState(channel["tx_state"])
                    row.rf_output_dbuv = channel["rf_output_dbuv"]
                    row.audio_underrun_count = channel["audio_underrun_count"]
                    row.rds_underrun_count = channel["rds_underrun_count"]
                    row.rds_fifo_free = channel["rds_fifo_free"]
                    row.hardware_last_error = channel["last_error_code"]
                    if row.hardware_tx_state == "ERROR":
                        errors.append(f"RP TX{row.id}: {row.hardware_last_error}")
        except DependencyUnavailable as exc:
            self.system.rp2354_state = "INCOMPATIBLE" if isinstance(exc, IncompatibleProtocol) else "UNRESPONSIVE"
            if self.real_rp:
                self.system.i2c_errors = self.rp.i2c_errors
                self.system.rp_uptime_ms = self.system.rp_spi_audio_link_error_count = None
                self.system.rp_last_error_code = None
                for key in ("rp_protocol_version", "rp_firmware_version", "hardware_revision"):
                    self.info[key] = None
                for row in self.modulators.statuses.values():
                    row.hardware_tx_state = None
                    row.tx_state = TxState.ERROR
                    row.rf_output_dbuv = None
                    row.audio_underrun_count = row.rds_underrun_count = row.rds_fifo_free = None
                    row.hardware_last_error = str(exc)
            errors.append(str(exc))
        try:
            mux_status = self.mux.get_status()
            self.system.mux_state = mux_status["state"]
            self.system.spi_errors = mux_status["spi_errors"]
            self.system.mux_transport = mux_status.get("transport", "mock")
            self.system.mux_transport_state = mux_status.get("transport_state", "RUNNING")
            self.system.mux_frames_sent = mux_status["frames_sent"]
            self.system.mux_mocked = mux_status.get("mocked", True)
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
        self.system.last_error = "; ".join(errors) or None

    def snapshot(self):
        # No dependency IO in the HTTP request path; refresh runs periodically.
        system = self.system.model_copy(deep=True)
        system.uptime_s = max(0, self.clock() - self.started)
        rows = []
        for status in self.modulators.statuses.values():
            row = status.model_copy(deep=True)
            source_id = self.modulators.active_sources.get(row.id)
            if source_id is not None and row.source_state != "ANALOG":
                row.source_state = self.sources.status(source_id).state.value
            rows.append(row)
        if system.last_error or any(r.tx_state == TxState.ERROR or r.source_state == "ERROR" or r.rds_last_error for r in rows):
            system.system_state = "DEGRADED"
        return HeadendStatus(system=system, modulators=rows)
