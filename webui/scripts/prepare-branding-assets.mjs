import { copyFile, mkdir } from 'node:fs/promises';

// Angular restricts asset inputs to its workspace. These are generated build
// inputs only; the approved repository-root assets remain the source of truth.
const source = new URL('../../assets/', import.meta.url);
const target = new URL('../tmp/branding-assets/', import.meta.url);
const files = [
  'fmheadend_logo_tower.svg',
  ...[16, 32, 48, 64, 128, 256].map((size) => `fmheadend_favicon_${size}.png`),
];
await mkdir(target, { recursive: true });
await Promise.all(files.map((file) => copyFile(new URL(file, source), new URL(file, target))));
