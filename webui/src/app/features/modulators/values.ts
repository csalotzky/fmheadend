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
// US RBDS programme types; diagnostic codes 30/31 are intentionally not selectable.
export const rbdsPtyLabels = [
  'None', 'News', 'Information', 'Sports', 'Talk', 'Rock', 'Classic Rock',
  'Adult Hits', 'Soft Rock', 'Top 40', 'Country', 'Oldies', 'Soft', 'Nostalgia',
  'Jazz', 'Classical', 'Rhythm and Blues', 'Soft Rhythm and Blues', 'Language',
  'Religious Music', 'Religious Talk', 'Personality', 'Public', 'College',
  'Unassigned', 'Unassigned', 'Unassigned', 'Unassigned', 'Unassigned', 'Weather',
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
// Fill from source keeps Unicode; only the Linux encoder maps it to RDS bytes.
export function rdsText(text: string, limit: number): string {
  return Array.from(text.normalize('NFC'), (char) =>
    char !== ' ' && /[\p{C}\p{Z}]/u.test(char) ? '?' : char,
  )
    .slice(0, limit)
    .join('');
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
// RDS Method A is independent of the startup tuning plan.
export const afOptions = Array.from({ length: 204 }, (_, i) => {
  const value = 87600 + i * 100;
  return { value, label: `${Math.floor(value / 1000)}.${(value % 1000) / 100} MHz` };
});
// Match checkText's Unicode code-point length validation, without truncation.
export const textLength = (value: string): number => Array.from(value).length;
