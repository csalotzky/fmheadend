import { SystemInfo } from '../../core/api/system-api';
export const systemInfo: SystemInfo = {
  frequency_min_khz: 76000,
  frequency_max_khz: 108000,
  frequency_step_khz: 50,
  fmheadend_version: '1.0.0',
  controller_protocol_version: 1,
  controller_firmware_version: 'mock',
  hardware_target: 'rp2354-qn8x-v1',
  modulator_count: 8,
  mocked: true,
  database_schema_version: 1,
  media_backend: 'MOCK',
  frequency_plan: {
    region: 1,
    region_name: 'Europe / standard',
    fm_min_khz: 87500,
    fm_max_khz: 108000,
    fm_step_khz: 100,
  },
};
