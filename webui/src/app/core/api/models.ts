// Display contracts from daemon/models/configuration.py and runtime_status.py.
export type TxState = 'OFF' | 'STARTING' | 'ON' | 'ERROR';
export interface RdsProfile {
  rds_pi: number;
  rds_ecc: number | null;
  rds_af_khz: number[];
  rds_ps: { text: string; duration_s: number }[];
  rds_pty: number;
  rds_tp: boolean;
  rds_rt: string;
  rds_rt_enabled: boolean;
  rds_rt_termination: 'CR' | 'PAD';
  rds_metadata_to_rt: boolean;
}
export interface Modulator extends RdsProfile {
  gain_mode: 'AUTO' | 'MANUAL';
  analog_gain_db: number;
  digital_trim_db: 0 | 1;
  soft_clipping: boolean;
  audio_mode: 'DIGITAL' | 'ANALOG';
  id: number;
  enabled: boolean;
  input_source_id: number | null;
  frequency_khz: number | null;
  stereo: boolean;
  preemphasis_us: 50 | 75;
  rf_attenuation_db: number | null;
  rds_enabled: boolean;
  rds_follow_source?: boolean;
  rds_override_profile_id?: number | null;
  rds_override_profile?: RdsProfile;
  timestamp: string;
}
export interface InputSource {
  rds_profile_id?: number | null;
  rds_profile?: RdsProfile | null;
  id: number;
  name: string;
  type: 'HTTP' | 'UDP' | 'RTP' | 'ALSA' | 'LOCAL' | 'PODCAST';
  config: Record<string, unknown>;
  processor_mode: 'bypass' | 'builtin' | 'stereotool';
  input_gain_db: number;
  audio_profile_path: string | null;
  gst_options: Record<string, unknown>;
  timestamp: string;
}
export interface ModulatorStatus {
  rds_rt_effective?: string | null;
  input_saturated: boolean | null;
  id: number;
  tx_state: TxState;
  hardware_tx_state: string | null;
  source_state: string | null;
  rf_attenuation_db: number | null;
  underruns: number;
  overruns: number;
  pcm_bytes_received: number;
  pcm_bytes_consumed: number;
  rds_state: string;
  rds_last_error: string | null;
  rds_last_error_scope?: 'TX' | 'GLOBAL';
  rds_groups_accepted: number;
  rds_text_ab: number;
  audio_underrun_count: number | null;
  rds_underrun_count: number | null;
  rds_fifo_free: number | null;
  hardware_last_error: string | null;
  last_error: string | null;
  last_error_scope?: 'TX' | 'GLOBAL';
}
export interface SystemStatus {
  global_stopped?: boolean;
  snapshot_age_s?: number;
  snapshot_stale?: boolean;
  rp_observation_stale?: boolean;
  rp_observation_age_s?: number | null;
  system_state: string;
  rp2354_state: string;
  mux_state: string;
  mux_transport: string;
  mux_transport_state: string;
  mux_frames_sent: number;
  mux_mocked: boolean;
  spi_errors: number;
  i2c_errors: number;
  rp_uptime_ms: number | null;
  rp_spi_audio_link_error_count: number | null;
  rp_last_error_code: string | null;
  uptime_s: number;
  cpu_percent: number | null;
  memory_percent: number | null;
  temperature_c: number | null;
  last_error: string | null;
  mocked: boolean;
}
export interface HeadendStatus {
  system: SystemStatus;
  modulators: ModulatorStatus[];
}
export interface ApiError {
  error: string;
  message: string;
}
export interface ChannelError extends ApiError {
  id: number;
}
export type LifecycleResult = ModulatorStatus | ChannelError;
export interface Snapshot {
  status: HeadendStatus;
  modulators: Modulator[];
  sources: InputSource[];
}
