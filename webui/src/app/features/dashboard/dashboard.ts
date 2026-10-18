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
    </div>
    <div class="channel-grid">
      @for (channel of state.channels(); track channel.id) {
        <app-channel-card
          [channel]="channel"
          [profileName]="
            channel.source && state.profileId(channel.source)
              ? state.profileNames()[state.profileId(channel.source)!]
              : null
          "
          [stale]="state.stale()"
          [locked]="state.busy() !== null"
          [pending]="state.isPending(channel.id)"
          [pendingAction]="state.pendingAction()"
          [commandState]="state.commandStates()[channel.id]"
          [feedback]="state.actionFeedback()[channel.id] || ''"
          (operate)="state.operate(channel.id, $event)"
        />
      } @empty {
        <p role="status">Transmitter inventory unavailable.</p>
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
}
