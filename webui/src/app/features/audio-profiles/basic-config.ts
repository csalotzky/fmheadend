// Matches daemon.models.configuration.BasicConfig. API validation remains authoritative.
export const highpassOptions = [20, 30, 40, 50, 60, 80, 100] as const;
export const lowpassOptions = [12000, 13000, 14000, 15000, 16000] as const;
export const ratioOptions = [1, 1.5, 2, 3, 4] as const;
export const thresholdOptions = Array.from({ length: 28 }, (_, i) => -30 + i);
export const eqRange = { min: -24, max: 12, step: 0.5 };
export interface BasicConfig {
  highpass_enabled: boolean;
  highpass_hz: (typeof highpassOptions)[number];
  eq_low_db: number;
  eq_mid_db: number;
  eq_high_db: number;
  compressor_enabled: boolean;
  compressor_threshold_dbfs: number;
  compressor_ratio: (typeof ratioOptions)[number];
  lowpass_enabled: boolean;
  lowpass_hz: (typeof lowpassOptions)[number];
  limiter_enabled: boolean;
}
export const basicDefaults: Readonly<BasicConfig> = {
  highpass_enabled: true,
  highpass_hz: 40,
  eq_low_db: 0,
  eq_mid_db: 0,
  eq_high_db: 0,
  compressor_enabled: true,
  compressor_threshold_dbfs: -12,
  compressor_ratio: 2,
  lowpass_enabled: true,
  lowpass_hz: 15000,
  limiter_enabled: true,
};
