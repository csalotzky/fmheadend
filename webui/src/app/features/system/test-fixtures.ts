import { SystemInfo } from '../../core/api/system-api';
export const systemInfo: SystemInfo = {
  fmheadend_version: '0.1.0',
  rp_protocol_version: 1,
  rp_firmware_version: 'mock',
  hardware_revision: 'mock',
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
