import { ProfilesList } from './features/audio-profiles/profiles-list';
import { ProfileEditor, profileDirtyGuard } from './features/audio-profiles/profile-editor';
import { ModulatorsList } from './features/modulators/modulators-list';
import { ModulatorEditor, modulatorDirtyGuard } from './features/modulators/modulator-editor';
import { SourcesList } from './features/sources/sources-list';
import { SourceEditor, sourceDirtyGuard } from './features/sources/source-editor';
import { Routes } from '@angular/router';
import { Dashboard } from './features/dashboard/dashboard';
import { SystemPage } from './features/system/system-page';
const pages: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
  { path: 'dashboard', component: Dashboard },
  { path: 'sources', component: SourcesList },
  { path: 'sources/new', component: SourceEditor, canDeactivate: [sourceDirtyGuard] },
  { path: 'sources/:id', component: SourceEditor, canDeactivate: [sourceDirtyGuard] },
  { path: 'modulators', component: ModulatorsList },
  { path: 'modulators/:id', component: ModulatorEditor, canDeactivate: [modulatorDirtyGuard] },
  { path: 'audio-profiles', component: ProfilesList },
  { path: 'audio-profiles/new', component: ProfileEditor, canDeactivate: [profileDirtyGuard] },
  { path: 'audio-profiles/edit', component: ProfileEditor, canDeactivate: [profileDirtyGuard] },
  { path: 'system', component: SystemPage },
  { path: '**', redirectTo: 'dashboard' },
];

// Every route uses the same product title; page headings remain independent.
export const routes: Routes = pages.map((route) => ({ ...route, title: 'FMheadend' }));
