import { SourcesList } from './features/sources/sources-list';
import { SourceEditor, sourceDirtyGuard } from './features/sources/source-editor';
import { Routes } from '@angular/router';
import { Dashboard } from './features/dashboard/dashboard';
import { Placeholder } from './layout/placeholder';
export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
  { path: 'dashboard', component: Dashboard, title: 'Dashboard | fmheadend' },
  { path: 'sources', component: SourcesList },
  { path: 'sources/new', component: SourceEditor, canDeactivate: [sourceDirtyGuard] },
  { path: 'sources/:id', component: SourceEditor, canDeactivate: [sourceDirtyGuard] },
  ...['modulators', 'audio-profiles', 'system'].map((path, i) => ({
    path,
    component: Placeholder,
    data: { title: ['Modulators', 'Audio Profiles', 'System'][i] },
  })),
  { path: 'modulators/:id', component: Placeholder, data: { title: 'Modulator configuration' } },
  { path: '**', redirectTo: 'dashboard' },
];
