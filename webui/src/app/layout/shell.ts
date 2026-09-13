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
