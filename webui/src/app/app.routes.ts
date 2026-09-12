import { Routes } from '@angular/router';
import { Dashboard } from './features/dashboard/dashboard';
import { Placeholder } from './layout/placeholder';
export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
  { path: 'dashboard', component: Dashboard, title: 'Dashboard | fmheadend' },
  ...['sources', 'modulators', 'audio-profiles', 'bandscan', 'system'].map((path, i) => ({
    path,
    component: Placeholder,
    data: { title: ['Sources', 'Modulators', 'Audio Profiles', 'Bandscan', 'System'][i] },
  })),
  { path: 'modulators/:id', component: Placeholder, data: { title: 'Modulator configuration' } },
  { path: '**', redirectTo: 'dashboard' },
];
