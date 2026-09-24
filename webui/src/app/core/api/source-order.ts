import { InputSource } from './models';

const names = new Intl.Collator('hu', { sensitivity: 'accent' });
export function sortSources(sources: readonly InputSource[]): InputSource[] {
  return [...sources].sort((a, b) => names.compare(a.name, b.name) || a.id - b.id);
}
