import { Component, inject } from '@angular/core';
import { DashboardState } from './dashboard-state';
import { ChannelCard } from './channel-card';
@Component({
  selector: 'app-dashboard',
  imports: [ChannelCard],
  template: ` <div class="page-title">
      <div>
        <p class="eyebrow">OPERATIONS</p>
        <h1>Dashboard</h1>
      </div>
      <span class="muted">8 physical channels &middot; 48 kHz digital audio</span>
    </div>
    <div class="channel-grid">
      @for (channel of state.channels(); track channel.id) {
        <app-channel-card
          [channel]="channel"
          [stale]="state.stale()"
          [locked]="state.busy() !== null"
          [pending]="state.busy() === channel.id"
          [actionError]="failure(channel.id)"
          (operate)="state.operate(channel.id, $event)"
        />
      }
    </div>`,
  styles: `
    .channel-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    @media (max-width: 1199px) {
      .channel-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    @media (max-width: 620px) {
      .channel-grid {
        grid-template-columns: 1fr;
      }
    }
  `,
})
export class Dashboard {
  readonly state = inject(DashboardState);
  failure(id: number) {
    return this.state.failures().find((f) => f.id === id)?.message ?? null;
  }
}
