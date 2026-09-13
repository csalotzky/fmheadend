import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { DashboardState } from '../dashboard/dashboard-state';
import { mhz } from './values';
@Component({
  selector: 'app-modulators-list',
  imports: [RouterLink],
  styleUrl: './modulators.css',
  template: `<h1>Modulators</h1>
    <p class="muted">
      Eight physical transmitters. Select a channel to configure RF, Audio and RDS.
    </p>
    @if (state.loading()) {
      <p role="status">Loading configuration...</p>
    }
    @if (state.stale()) {
      <p class="warning" role="status">Backend unavailable. Displayed information may be stale.</p>
    }
    <div class="overview">
      @for (channel of state.channels(); track channel.id) {
        <section [attr.aria-label]="'TX ' + channel.id" class="summary">
          <div>
            <h2>TX {{ channel.id }}</h2>
            <strong>{{
              channel.config?.frequency_khz != null
                ? frequency(channel.config!.frequency_khz) + ' MHz'
                : 'Not configured'
            }}</strong>
          </div>
          <div>
            {{ channel.config?.audio_mode ?? 'Unavailable' }}
            <p class="muted">{{ channel.sourceName }}</p>
            <span
              >RDS
              {{
                channel.config ? (channel.config.rds_enabled ? 'On' : 'Off') : 'Unavailable'
              }}</span
            >
          </div>
          <div [class.error]="channel.runtime?.hardware_tx_state === 'ERROR'">
            Reported state:
            {{ state.stale() ? 'UNKNOWN' : (channel.runtime?.hardware_tx_state ?? 'UNKNOWN') }}
          </div>
          <a class="button-link" [routerLink]="['/modulators', channel.id]"
            >Edit TX {{ channel.id }}</a
          >
          @if (channel.error) {
            <p class="error summary-error" role="status">{{ channel.error }}</p>
          }
        </section>
      }
    </div>`,
})
export class ModulatorsList {
  readonly state = inject(DashboardState);
  readonly frequency = mhz;
}
