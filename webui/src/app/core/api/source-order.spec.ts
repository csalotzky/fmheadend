import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter, ActivatedRoute, convertToParamMap } from '@angular/router';
import { sortSources } from './source-order';
import { sample } from '../../features/dashboard/test-fixtures';
import { DashboardState } from '../../features/dashboard/dashboard-state';
import { SourcesList } from '../../features/sources/sources-list';
import { ModulatorEditor } from '../../features/modulators/modulator-editor';

it('uses the same Hungarian, case-insensitive ordering in list and selector after refresh', () => {
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideRouter([]),
      {
        provide: ActivatedRoute,
        useValue: { snapshot: { paramMap: convertToParamMap({ id: '0' }) } },
      },
    ],
  });
  const data = sample();
  data.sources = ['zene', 'Öböl', 'Óra', 'csiga', 'cukor', 'ár', 'Ár', 'alma'].map((name, i) => ({
    ...data.sources[0],
    name,
    id: 8 - i,
  }));
  const original = data.sources.map((s) => s.id);
  const shared = TestBed.inject(DashboardState);
  shared.data.set(data);
  const list = TestBed.createComponent(SourcesList).componentInstance;
  const editor = TestBed.createComponent(ModulatorEditor).componentInstance;
  expect(list.sources().map((s) => s.name)).toEqual([
    'alma',
    'Ár',
    'ár',
    'cukor',
    'csiga',
    'Óra',
    'Öböl',
    'zene',
  ]);
  expect(editor.sources()).toEqual(list.sources());
  expect(data.sources.map((s) => s.id)).toEqual(original);
  const renamed = data.sources.map((s) => (s.id === 8 ? { ...s, name: 'A' } : s));
  shared.data.set({ ...data, sources: [...renamed, { ...data.sources[0], id: 0, name: 'Ár' }] });
  expect(list.sources()[0].id).toBe(8);
  expect(editor.sources()).toEqual(sortSources(shared.data()!.sources));
  expect(
    list
      .sources()
      .filter((s) => s.name.toLowerCase() === 'ár')
      .map((s) => s.id),
  ).toEqual([0, 2, 3]);
});
