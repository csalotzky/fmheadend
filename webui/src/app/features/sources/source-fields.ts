import { SourceType } from './source-api';
export interface Field {
  key: string;
  label: string;
  help?: string;
  required?: boolean;
  numeric?: boolean;
  min?: number;
  max?: number;
  options?: string[];
}
const defaultHelp = 'Optional. Leave blank to use the default.';
const network: Field[] = [
  { key: 'host', label: 'Listen address', help: defaultHelp },
  { key: 'port', label: 'Port', required: true, numeric: true, min: 1, max: 65535 },
  { key: 'multicast_group', label: 'Multicast group (optional)' },
];
export const fields: Record<SourceType, Field[]> = {
  HTTP: [
    { key: 'uri', label: 'Stream URL (HTTP / HTTPS / HLS)', required: true },
    { key: 'buffer_ms', label: 'Buffer (ms)', help: defaultHelp, numeric: true, min: 0 },
    {
      key: 'connect_timeout_ms',
      label: 'Connection timeout (ms)',
      help: defaultHelp,
      numeric: true,
      min: 1,
    },
  ],
  UDP: [
    ...network,
    {
      key: 'format',
      label: 'Raw audio format',
      required: true,
      options: ['S16LE', 'S16BE', 'S24LE', 'S24BE', 'S32LE', 'S32BE', 'F32LE', 'F32BE', 'U8'],
    },
    { key: 'sample_rate', label: 'Input sample rate (Hz)', required: true, numeric: true, min: 1 },
    { key: 'channels', label: 'Input channels', required: true, numeric: true, min: 1, max: 8 },
  ],
  RTP: [
    ...network,
    { key: 'codec', label: 'RTP codec', required: true, options: ['PCM', 'MP3', 'AAC', 'OPUS'] },
    {
      key: 'payload_type',
      label: 'RTP payload type',
      required: true,
      numeric: true,
      min: 0,
      max: 127,
    },
    { key: 'clock_rate', label: 'RTP clock rate (optional)', numeric: true, min: 1 },
    { key: 'channels', label: 'Input channels (optional)', numeric: true, min: 1, max: 8 },
    { key: 'codec_data', label: 'Codec initialization data (hex, optional)' },
  ],
  ALSA: [
    { key: 'device', label: 'ALSA device (e.g. plughw:2,0)', required: true },
    {
      key: 'sample_rate',
      label: 'Input sample rate (Hz)',
      help: defaultHelp,
      numeric: true,
      min: 1,
    },
    {
      key: 'channels',
      label: 'Input channels',
      help: defaultHelp,
      numeric: true,
      min: 1,
      max: 8,
    },
  ],
};
