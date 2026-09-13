import { combiningMarks } from './combining-marks';
// Presentation conversions follow configuration.py and rds/characters.py.
export const ptyLabels = [
  'None',
  'News',
  'Current Affairs',
  'Information',
  'Sport',
  'Education',
  'Drama',
  'Culture',
  'Science',
  'Varied',
  'Pop Music',
  'Rock Music',
  'Easy Listening',
  'Light Classical',
  'Serious Classical',
  'Other Music',
  'Weather',
  'Finance',
  "Children's Programmes",
  'Social Affairs',
  'Religion',
  'Phone In',
  'Travel',
  'Leisure',
  'Jazz Music',
  'Country Music',
  'National Music',
  'Oldies Music',
  'Folk Music',
  'Documentary',
];
export function mhz(khz: number | null): string {
  if (khz === null) return '';
  return `${Math.floor(khz / 1000)}.${String(khz % 1000).padStart(3, '0')}`
    .replace(/0+$/, '')
    .replace(/\.$/, '');
}
export function khz(raw: string, optional = false): number | null {
  const text = raw.trim();
  if (!text && optional) return null;
  const match = /^(\d+)(?:\.(\d{1,3}))?$/.exec(text);
  if (!match) throw new Error('Enter MHz with at most three decimal places.');
  const value = Number(match[1]) * 1000 + Number((match[2] ?? '').padEnd(3, '0'));
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error('Enter a positive frequency.');
  return value;
}
export function hex(value: number | null, width: number): string {
  return value === null ? '' : value.toString(16).toUpperCase().padStart(width, '0');
}
export function parseHex(raw: string, width: number, optional = false): number | null {
  const text = raw.trim().replace(/^0x/i, '');
  if (!text && optional && !raw.trim()) return null;
  if (!new RegExp(`^[0-9a-fA-F]{1,${width}}$`).test(text))
    throw new Error(`Enter ${width === 4 ? 'PI' : 'ECC'} as up to ${width} hexadecimal digits.`);
  return Number.parseInt(text, 16);
}
const replacements: Record<string, string> = {
  '\u2018': "'",
  '\u2019': "'",
  '\u201c': '"',
  '\u201d': '"',
  '\u2013': '-',
  '\u2014': '-',
  '\u00df': 'ss',
  '\u00c6': 'AE',
  '\u00e6': 'ae',
  '\u0152': 'OE',
  '\u0153': 'oe',
  '\u00d8': 'O',
  '\u00f8': 'o',
};
export function rdsText(text: string, limit: number): string {
  let result = '';
  for (const char of text.normalize('NFC')) {
    const plain = (replacements[char] ?? char).normalize('NFKD').replace(combiningMarks, '');
    for (const c of plain) {
      const code = c.codePointAt(0)!;
      result += code >= 32 && code < 127 && ![36, 94, 96, 126].includes(code) ? c : '?';
    }
    if (result.length >= limit) break;
  }
  return result.slice(0, limit);
}
export function checkText(text: string, max: number, required = false): string {
  if (
    (required && !text.length) ||
    Array.from(text).length > max ||
    Array.from(text).some((c) => c !== ' ' && /[\p{C}\p{Z}]/u.test(c))
  )
    throw new Error(
      `RDS text must contain ${required ? '1' : '0'} to ${max} printable characters.`,
    );
  return text;
}
export function decimal(
  raw: string,
  label: string,
  min: number,
  max: number,
  optional = false,
): number | null {
  if (!raw.trim() && optional) return null;
  if (!/^-?\d+(?:\.\d)?$/.test(raw.trim()))
    throw new Error(`${label}: enter a number in 0.1 steps.`);
  const n = Number(raw);
  if (!Number.isFinite(n) || n < min || n > max)
    throw new Error(`${label}: enter a value between ${min} and ${max}.`);
  return n;
}

// TODO: Make FM band limits and raster region-dependent in a later regional policy.
export const frequencyOptions = Array.from({ length: 206 }, (_, i) => {
  const khz = 87500 + i * 100;
  return { value: khz, label: `${Math.floor(khz / 1000)}.${(khz % 1000) / 100} MHz` };
});
export const rfOutputOptions = Array.from({ length: 16 }, (_, i) => ({
  value: (i * 15) / 10,
  label: i === 0 ? 'Maximum' : `-${Math.floor((i * 15) / 10)}.${(i * 15) % 10} dB`,
}));

export const analogGainOptions = [-1.5, 0, 1.5, 3, 4.5, 6, 7.5, 9, 10.5, 12, 13.5, 15].map(
  (value) => ({
    value: String(value),
    label: `${value > 0 ? '+' : ''}${value.toFixed(1)} dB`,
  }),
);
export const afOptions = frequencyOptions.filter(
  (option) => option.value >= 87600 && option.value <= 107900,
);
// Match checkText's Unicode code-point length validation, without truncation.
export const textLength = (value: string): number => Array.from(value).length;
