import { effect } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Router, NavigationEnd } from '@angular/router';
import { SystemApi } from '../core/api/system-api';
import { ProfileApi } from '../features/audio-profiles/profile-api';
import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { DashboardState } from '../features/dashboard/dashboard-state';
@Component({
  selector: 'app-shell',
  imports: [DatePipe, RouterLink, RouterLinkActive, RouterOutlet],
  templateUrl: './shell.html',
  styleUrl: './shell.css',
})
export class Shell implements OnInit, OnDestroy {
  readonly state = inject(DashboardState);
  readonly menu = signal(false);
  readonly version = signal<string | null>(null);
  private systemApi = inject(SystemApi);
  private profiles = inject(ProfileApi);
  constructor() {
    inject(Router)
      .events.pipe(takeUntilDestroyed())
      .subscribe((event) => {
        if (event instanceof NavigationEnd) this.state.clearMessages();
      });
    effect((cleanup) => {
      this.state.data();
      const info = this.systemApi.info().subscribe({
        next: (i) => this.version.set(i.fmheadend_version),
        error: () => this.version.set(null),
      });
      const profiles = this.profiles.list().subscribe({
        next: (rows) => {
          this.state.profileNames.set(Object.fromEntries(rows.map((p) => [p.path, p.name])));
          this.state.defaultAudioProfileId.set(rows.find((p) => p.is_system_default)?.path ?? null);
        },
        error: () => this.state.profileNames.set({}),
      });
      cleanup(() => {
        info.unsubscribe();
        profiles.unsubscribe();
      });
    });
  }
  hostMetric(value: number | null | undefined, suffix: string): string {
    return this.state.backendUnavailable() || value == null || !Number.isFinite(value)
      ? '\u2014'
      : `${Math.round(value)}${suffix}`;
  }
  readonly links = [
    ['dashboard', 'Dashboard'],
    ['sources', 'Sources'],
    ['modulators', 'Modulators'],
    ['audio-profiles', 'Audio Profiles'],
    ['system', 'System'],
  ];
  ngOnInit() {
    this.state.start();
  }
  ngOnDestroy() {
    this.state.stop();
  }
}
