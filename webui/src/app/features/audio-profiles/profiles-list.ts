import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom, forkJoin } from 'rxjs';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
import { InputSource } from '../../core/api/models';
import { errorMessage } from '../../core/api/headend-api';
import { AudioProfile, ProfileApi, processorLabel } from './profile-api';
@Component({
  host: { class: 'page-content' },
  selector: 'app-profiles-list',
  imports: [RouterLink],
  styleUrl: '../sources/sources.css',
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
      <p>No audio profiles. Add a profile to configure source gain.</p>
    }
    @for (profile of profiles(); track profile.id) {
      <section class="source-row">
        <div class="source-summary">
          <strong>{{ profile.name }}</strong
          ><span>{{ label(profile.processor) }}</span
          ><span>Gain: {{ profile.gain_db }} dB</span>
        </div>
        <div class="actions">
          <span>Used by {{ usedBy(profile.id) }} sources</span
          ><a class="action-link" [routerLink]="['/audio-profiles', profile.id]">Edit</a
          ><button type="button" [disabled]="deleting() !== null" (click)="remove(profile)">
            Delete
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
  readonly deleting = signal<number | null>(null);
  readonly label = processorLabel;
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
  usedBy(id: number) {
    return this.sources().filter((s) => s.audio_profile_id === id).length;
  }
  async remove(profile: AudioProfile) {
    if (this.deleting() !== null || !window.confirm(`Delete audio profile "${profile.name}"?`))
      return;
    this.deleting.set(profile.id);
    this.error.set('');
    try {
      await firstValueFrom(this.api.delete(profile.id));
      await this.load();
    } catch (e) {
      this.error.set(errorMessage(e));
    } finally {
      this.deleting.set(null);
    }
  }
}
