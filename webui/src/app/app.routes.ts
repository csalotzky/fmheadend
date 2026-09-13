import { ModulatorsList } from './features/modulators/modulators-list';
import { ModulatorEditor, modulatorDirtyGuard } from './features/modulators/modulator-editor';
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
  { path: 'modulators', component: ModulatorsList },
  { path: 'modulators/:id', component: ModulatorEditor, canDeactivate: [modulatorDirtyGuard] },
  ...['audio-profiles', 'system'].map((path, i) => ({
    path,
    component: Placeholder,
    data: { title: ['Audio Profiles', 'System'][i] },
  })),
  { path: '**', redirectTo: 'dashboard' },
];
