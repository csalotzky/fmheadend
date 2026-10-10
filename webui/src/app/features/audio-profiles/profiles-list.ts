import { Component, OnInit, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { firstValueFrom, forkJoin } from 'rxjs';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
import { InputSource } from '../../core/api/models';
import { errorMessage } from '../../core/api/headend-api';
import { AudioProfile, ProfileApi } from './profile-api';
@Component({
  host: { class: 'page-content' },
  selector: 'app-profiles-list',
  imports: [RouterLink],
  styleUrl: '../sources/sources.css',
  styles: `
    .profile-summary {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem 2rem;
      align-items: baseline;
    }
    .profile-summary small {
      flex-basis: 100%;
      color: var(--muted);
    }
    .profile-summary span {
      overflow-wrap: anywhere;
      color: var(--muted);
    }
  `,
  template: `
    <div class="page-header">
      <h1>Audio Profiles</h1>
      <a class="action-link" routerLink="/audio-profiles/new">Add Audio Profile</a>
    </div>
    @if (error()) {
      <p role="alert" class="error">{{ error() }}</p>
    }
    @if (loading()) {
      <p role="status">Loading audio profiles...</p>
    } @else if (!error() && !profiles().length) {
      <p>No audio profiles. Add a Built-in DSP profile.</p>
    }
    @for (profile of profiles(); track profile.path) {
      <section class="source-row">
        <div class="profile-summary">
          <strong>{{ profile.name }}</strong
          ><span>{{ profile.path }}</span>
          <small>{{ profile.description }}</small>
        </div>
        <div class="actions">
          <span>Used by {{ usedBy(profile.path) }} sources</span>
          @if (profile.kind === 'default') {
            <a
              class="action-link"
              routerLink="/audio-profiles/edit"
              [queryParams]="{ path: profile.path }"
              >View</a
            >
          }
          @if (profile.kind === 'custom') {
            <a
              class="action-link"
              routerLink="/audio-profiles/edit"
              [queryParams]="{ path: profile.path }"
              >Edit</a
            >
            <button
              type="button"
              [disabled]="deleting() !== null || usedBy(profile.path) > 0"
              (click)="remove(profile)"
            >
              Delete
            </button>
          }
          <button type="button" [disabled]="deleting() !== null" (click)="clone(profile)">
            Clone
          </button>
        </div>
      </section>
    }
  `,
})
export class ProfilesList implements OnInit {
  private api = inject(ProfileApi);
  private http = inject(HttpClient);
  readonly profiles = signal<AudioProfile[]>([]);
  readonly sources = signal<InputSource[]>([]);
  readonly loading = signal(false);
  readonly error = signal('');
  readonly deleting = signal<string | null>(null);
  private router = inject(Router);
  ngOnInit() {
    void this.load();
  }
  async load() {
    this.loading.set(true);
    this.error.set('');
    try {
      const data = await firstValueFrom(
        forkJoin({
          profiles: this.api.list(),
          sources: this.http.get<InputSource[]>('/api/v1/sources').pipe(timeout(15000)),
        }),
      );
      this.profiles.set(data.profiles);
      this.sources.set(data.sources);
    } catch (e) {
      this.error.set(errorMessage(e));
    } finally {
      this.loading.set(false);
    }
  }
  usedBy(path: string) {
    return this.sources().filter((s) => s.audio_profile_path === path).length;
  }
  async clone(profile: AudioProfile) {
    if (this.deleting() !== null) return;
    const name = window.prompt('Name for cloned Audio Profile', profile.name + ' copy');
    if (!name?.trim()) return;
    this.deleting.set(profile.path);
    try {
      const result = await firstValueFrom(this.api.clone(profile.path, name));
      await this.router.navigate(['/audio-profiles/edit'], { queryParams: { path: result.path } });
    } catch (e) {
      this.error.set(errorMessage(e));
    } finally {
      this.deleting.set(null);
    }
  }
  async remove(profile: AudioProfile) {
    if (profile.kind === 'default' || this.usedBy(profile.path)) return;
    if (this.deleting() !== null || !window.confirm(`Delete audio profile "${profile.name}"?`))
      return;
    this.deleting.set(profile.path);
    this.error.set('');
    try {
      await firstValueFrom(this.api.delete(profile.path));
      await this.load();
    } catch (e) {
      this.error.set(errorMessage(e));
    } finally {
      this.deleting.set(null);
    }
  }
}
