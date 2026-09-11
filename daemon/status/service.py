import time
from ..errors import DependencyUnavailable
from ..models.runtime_status import HeadendStatus, SystemStatus, TxState


class StatusService:
    def __init__(self, modulators, sources, rp, mux, clock=time.monotonic):
        self.modulators, self.sources, self.rp, self.mux = modulators, sources, rp, mux
        self.clock, self.started = clock, clock()
        self.system = SystemStatus()
        self.refresh()

    def refresh(self):
        errors = []
        try:
            rp_status = self.rp.get_status()
            self.system.rp2354_state = rp_status["state"]
            self.system.i2c_errors = rp_status["i2c_errors"]
        except DependencyUnavailable as exc:
            self.system.rp2354_state = "UNRESPONSIVE"
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
        if system.last_error or any(r.tx_state == TxState.ERROR or r.source_state == "ERROR" for r in rows):
            system.system_state = "DEGRADED"
        return HeadendStatus(system=system, modulators=rows)
