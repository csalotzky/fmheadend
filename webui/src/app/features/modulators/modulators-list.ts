import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ChannelView, DashboardState } from '../dashboard/dashboard-state';
import { mhz } from './values';
@Component({
  host: { class: 'page-content' },
  selector: 'app-modulators-list',
  imports: [RouterLink],
  styleUrl: '../../shared/list-layout.css',
  template: `<div class="page-header">
      <div>
        <h1>Modulators</h1>
        <p class="list-intro">
          Eight physical transmitters. Select a channel to configure RF, Audio and RDS.
        </p>
      </div>
    </div>
    @if (state.loading()) {
      <p role="status">Loading configuration...</p>
    }
    @if (state.stale()) {
      <p class="warning" role="status">Backend unavailable. Displayed information may be stale.</p>
    }
    <div class="overview">
      @for (channel of state.channels(); track channel.id) {
        <section
          [attr.aria-label]="'TX ' + channel.id"
          class="summary list-card list-grid list-grid--four"
          [attr.data-reported-state]="reportedState(channel)"
        >
          <div class="list-identity">
            <h2 class="list-primary">
              <a [routerLink]="['/modulators', channel.id]">TX {{ channel.id }}</a>
            </h2>
            <p class="list-secondary">
              {{
                channel.config?.frequency_khz != null
                  ? frequency(channel.config!.frequency_khz) + ' MHz'
                  : 'Not configured'
              }}
            </p>
          </div>
          <div class="list-description">
            <strong class="list-primary">{{ channel.sourceName }}</strong>
            <p class="list-secondary">
              {{ channel.config?.audio_mode ?? 'Unavailable' }} / RDS
              {{ channel.config ? (channel.config.rds_enabled ? 'On' : 'Off') : 'Unavailable' }}
            </p>
          </div>
          <div class="list-status" [class.error]="reportedState(channel) === 'ERROR'">
            Reported state: {{ reportedState(channel) }}
          </div>
          <div class="actions list-actions">
            <a class="button-link" [routerLink]="['/modulators', channel.id]"
              >Edit TX {{ channel.id }}</a
            >
          </div>
          @if (channel.error) {
            <p class="error list-detail" role="status">{{ channel.error }}</p>
          }
        </section>
      }
    </div>`,
})
export class ModulatorsList {
  readonly state = inject(DashboardState);
  readonly frequency = mhz;
  reportedState(channel: ChannelView): string {
    if (this.state.stale() || channel.observationStale) return 'UNKNOWN';
    return channel.runtime?.hardware_tx_state || 'UNKNOWN';
  }
}
