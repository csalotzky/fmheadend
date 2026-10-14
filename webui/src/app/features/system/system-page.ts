import { Component, DestroyRef, computed, effect, inject, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { Subscription, finalize } from 'rxjs';
import { DashboardState } from '../dashboard/dashboard-state';
import { SystemApi, SystemInfo, RecentLog, frequencyLabel } from '../../core/api/system-api';
import { errorMessage } from '../../core/api/headend-api';
@Component({
  selector: 'app-system-page',
  imports: [DatePipe],
  host: { class: 'page-content' },
  styleUrl: './system-page.css',
  template: `
    <header class="page-header">
      <h1>System</h1>
    </header>
    @if (shared.stale()) {
      <p class="warning" role="status">
        Backend unavailable. Observations are unavailable until the next successful refresh.
      </p>
    }
    @if (error()) {
      <p class="warning" role="status">System information unavailable: {{ error() }}</p>
    }
    <div class="system-sections">
      <section>
        <h2>Backend</h2>
        <dl>
          <dt>Status</dt>
          <dd>{{ runtime()?.system_state ?? 'Unavailable' }}</dd>
          <dt>Version</dt>
          <dd>{{ info()?.fmheadend_version ?? 'Unavailable' }}</dd>
          <dt>Media backend</dt>
          <dd>{{ info()?.media_backend ?? 'Unavailable' }}</dd>
        </dl>
      </section>
      <section>
        <h2>Controller</h2>
        <dl>
          <dt>State</dt>
          <dd>
            {{ runtime()?.controller_state ?? 'Unavailable' }}{{ runtime()?.mocked ? ' (mock)' : '' }}
          </dd>
          <dt>Protocol</dt>
          <dd>
            {{ rpAvailable() ? (info()?.controller_protocol_version ?? 'Unavailable') : 'Unavailable' }}
          </dd>
          <dt>Firmware</dt>
          <dd>
            {{ rpAvailable() ? (info()?.controller_firmware_version ?? 'Unavailable') : 'Unavailable' }}
          </dd>
          <dt>Hardware</dt>
          <dd>
            {{ rpAvailable() ? (info()?.hardware_revision ?? 'Unavailable') : 'Unavailable' }}
          </dd>
        </dl>
      </section>
      <section>
        <h2>Multiplexer</h2>
        <dl>
          <dt>State</dt>
          <dd>{{ runtime()?.mux_state ?? 'Unavailable' }}</dd>
          <dt>Transport</dt>
          <dd>
            {{ runtime()?.mux_transport ?? 'Unavailable'
            }}{{ runtime()?.mux_mocked ? ' (mock)' : '' }}
          </dd>
          <dt>Transport state</dt>
          <dd>{{ runtime()?.mux_transport_state ?? 'Unavailable' }}</dd>
          <dt>Frames sent</dt>
          <dd>
            {{ runtime()?.mux_state === 'RUNNING' ? runtime()?.mux_frames_sent : 'Unavailable' }}
          </dd>
        </dl>
      </section>
      <section>
        <h2>FM frequency plan</h2>
        @if (info(); as data) {
          <dl>
            <dt>Region</dt>
            <dd>{{ data.frequency_plan.region }} - {{ data.frequency_plan.region_name }}</dd>
            <dt>FM band</dt>
            <dd>
              {{ frequency(data.frequency_plan.fm_min_khz).replace(' MHz', '') }}-{{
                frequency(data.frequency_plan.fm_max_khz)
              }}
            </dd>
            <dt>Step</dt>
            <dd>{{ data.frequency_plan.fm_step_khz }} kHz</dd>
          </dl>
        } @else {
          <p>Unavailable</p>
        }
      </section>
      <section class="diagnostics">
        <h2>Diagnostics</h2>
        @if (runtime(); as status) {
          <dl>
            <dt>I2C errors</dt>
            <dd>{{ status.i2c_errors }}</dd>
            <dt>SPI errors</dt>
            <dd>{{ status.spi_errors }}</dd>
            <dt>Controller audio-link errors</dt>
            <dd>{{ status.controller_spi_audio_link_error_count ?? 'Unavailable' }}</dd>
            <dt>Last Controller error (history)</dt>
            <dd>
              {{
                status.controller_last_error_code ??
                  (!status.mocked &&
                  status.controller_communication_state === 'AVAILABLE' &&
                  status.controller_observation_stale === false &&
                  status.controller_observation_age_s != null &&
                  status.snapshot_stale !== true
                    ? 'None'
                    : 'Unavailable')
              }}
            </dd>
          </dl>
          @for (channel of shared.channels(); track channel.id) {
            @if (channel.error) {
              <p class="error">TX {{ channel.id }}: {{ channel.error }}</p>
            }
          }
        } @else {
          <p>Runtime diagnostics unavailable.</p>
        }
        <div class="log-heading">
          <h3>Recent backend logs</h3>
          <button [disabled]="logsLoading()" (click)="refreshLogs()">Refresh logs</button>
        </div>
        <p class="muted">Latest 100 entries</p>
        @if (logsError()) {
          <p class="warning" role="status">Logs unavailable: {{ logsError() }}</p>
        } @else if (logsLoading() && !logs().length) {
          <p role="status">Loading logs…</p>
        } @else if (!logs().length) {
          <p>No recent backend log entries.</p>
        }
        <div class="recent-logs">
          @for (entry of logs(); track $index) {
            <div class="log-entry">
              <time [attr.datetime]="entry.timestamp">{{
                entry.timestamp | date: 'yyyy-MM-dd HH:mm:ss'
              }}</time>
              <strong>{{ entry.severity }}</strong>
              <span class="log-source">{{ entry.logger }}</span>
              <pre>{{ entry.message }}</pre>
            </div>
          }
        </div>
      </section>
    </div>
  `,
})
export class SystemPage {
  readonly shared = inject(DashboardState);
  private api = inject(SystemApi);
  readonly info = signal<SystemInfo | null>(null);
  readonly error = signal('');
  readonly logs = signal<RecentLog[]>([]);
  readonly logsError = signal('');
  readonly logsLoading = signal(false);
  private logsRequest?: Subscription;
  refreshLogs() {
    if (this.logsLoading()) return;
    this.logsLoading.set(true);
    this.logsRequest = this.api
      .logs()
      .pipe(finalize(() => this.logsLoading.set(false)))
      .subscribe({
        next: (entries) => {
          this.logs.set(entries.slice(0, 100));
          this.logsError.set('');
        },
        error: (e) => {
          this.logs.set([]);
          this.logsError.set(errorMessage(e));
        },
      });
  }
  readonly runtime = computed(() =>
    this.shared.stale() ? null : this.shared.data()?.status.system,
  );
  readonly rpAvailable = computed(
    () =>
      !!this.runtime() && !['UNRESPONSIVE', 'INCOMPATIBLE'].includes(this.runtime()!.controller_state),
  );
  readonly frequency = frequencyLabel;
  constructor() {
    inject(DestroyRef).onDestroy(() => this.logsRequest?.unsubscribe());
    effect(() => {
      this.shared.data(); // Reuse general polling, no System-only timer.
      untracked(() => this.refreshLogs());
    });
    effect((cleanup) => {
      this.shared.data();
      const sub = this.api.info().subscribe({
        next: (value) => {
          this.info.set(value);
          this.error.set('');
        },
        error: (e) => {
          this.info.set(null);
          this.error.set(errorMessage(e));
        },
      });
      cleanup(() => sub.unsubscribe());
    });
  }
}
